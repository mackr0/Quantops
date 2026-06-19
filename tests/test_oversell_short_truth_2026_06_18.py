"""2026-06-18 — order-id truth: a position is the signed sum of its own
filled orders, and a FILLED stock sell ALWAYS moves the net. A real stock
oversell (sold more shares than were ever bought) must surface as a short
in get_virtual_positions, never be dropped as an "orphan artifact" — the
UWMC incident (10 profiles, ~$187K of phantom equity) happened because the
dropped short left its cash proceeds in equity with no offsetting position.

This pins the invariant: get_virtual_positions' net per stock symbol ==
Σ(filled buys) − Σ(filled sells). It also keeps the cases the old drop
protected: a completed round-trip stays FLAT (no phantom short), and a
status-flip close with no sell can't spawn a phantom long.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


@pytest.fixture
def db(tmp_path):
    from journal import init_db
    p = str(tmp_path / "p.db")
    init_db(p)
    return p


def _seed(db, rows):
    """rows: (symbol, side, qty, price, status)"""
    c = sqlite3.connect(db)
    for i, (sym, side, qty, px, status) in enumerate(rows):
        c.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, "
            "fill_price, status, order_id) VALUES (?,?,?,?,?,?,?,?)",
            ("2026-06-18T13:00:0%d" % (i % 10), sym, side, qty, px, px,
             status, "oid-%s-%s-%d-%d" % (sym, side, int(qty), i)),
        )
    c.commit()
    c.close()


def _net(db, sym):
    from journal import get_virtual_positions
    tot = 0.0
    for p in get_virtual_positions(db):
        if p.get("symbol") == sym and not p.get("occ_symbol"):
            tot += float(p.get("qty") or 0)
    return tot


def _raw_signed_net(db, sym):
    """Order-id truth: Σ(filled buy/cover) − Σ(filled sell/short)."""
    c = sqlite3.connect(db)
    n = c.execute(
        "SELECT COALESCE(SUM(CASE WHEN side IN ('buy','cover') THEN qty "
        "WHEN side IN ('sell','short') THEN -qty ELSE 0 END),0) "
        "FROM trades WHERE symbol=? AND occ_symbol IS NULL AND "
        "COALESCE(status,'open') NOT IN ('canceled','expired','rejected',"
        "'done_for_day','pending_protective','auto_reconciled_phantom_close')",
        (sym,)).fetchone()[0]
    c.close()
    return float(n or 0)


def test_stock_oversell_surfaces_as_short(db):
    # The UWMC shape: buy N (closed), the real close sell N (closed),
    # then a SECOND sell N (closed) that sold shares already gone.
    _seed(db, [
        ("UWMC", "buy", 20634, 2.47, "closed"),
        ("UWMC", "sell", 20634, 2.29, "closed"),
        ("UWMC", "sell", 20634, 2.18, "closed"),
    ])
    assert _net(db, "UWMC") == -20634.0          # real short, not dropped
    assert _net(db, "UWMC") == _raw_signed_net(db, "UWMC")  # == order-id truth


def test_completed_round_trip_stays_flat(db):
    # buy then sell equal qty (both closed) — a clean round trip is FLAT,
    # must NOT flash a phantom short.
    _seed(db, [
        ("AAA", "buy", 100, 10.0, "closed"),
        ("AAA", "sell", 100, 11.0, "closed"),
    ])
    assert _net(db, "AAA") == 0.0
    assert _net(db, "AAA") == _raw_signed_net(db, "AAA")


def test_open_long_unaffected(db):
    _seed(db, [("BBB", "buy", 50, 5.0, "open")])
    assert _net(db, "BBB") == 50.0


def test_partial_oversell_books_only_the_excess(db):
    # buy 100 (open) held; sell 150 oversells by 50 → long gone, short 50.
    _seed(db, [
        ("CCC", "buy", 100, 4.0, "open"),
        ("CCC", "sell", 150, 4.1, "closed"),
    ])
    assert _net(db, "CCC") == -50.0
    assert _net(db, "CCC") == _raw_signed_net(db, "CCC")


def test_status_flip_close_no_sell_does_not_spawn_phantom_long(db):
    # A buy flipped to 'closed' with NO matching sell (reconciliation
    # status-flip): the broker is flat. The closed-long qty is inert — it
    # can only absorb sells, never create a long — so net stays 0.
    _seed(db, [("DDD", "buy", 100, 3.0, "closed")])
    assert _net(db, "DDD") == 0.0


def test_two_buys_one_oversell(db):
    # buy 100 + buy 100 (one open, one closed), sell 250 → over by 50.
    _seed(db, [
        ("EEE", "buy", 100, 2.0, "closed"),
        ("EEE", "buy", 100, 2.0, "open"),
        ("EEE", "sell", 250, 2.1, "closed"),
    ])
    assert _net(db, "EEE") == -50.0
    assert _net(db, "EEE") == _raw_signed_net(db, "EEE")


def test_add_then_trim_keeps_held_remainder(db):
    # The case the first (closed_long_qty) attempt got WRONG: buy 100,
    # buy 50, sell 100 → reconcile flips the first buy to 'closed' (FIFO-
    # matched by the sell), the 50 stays held. Including the closed buy in
    # the FIFO timeline makes the sell match it in order, leaving +50.
    _seed(db, [
        ("FFF", "buy", 100, 2.0, "closed"),   # oldest — matched by the sell
        ("FFF", "buy", 50, 2.0, "open"),       # still held
        ("FFF", "sell", 100, 2.1, "closed"),
    ])
    assert _net(db, "FFF") == 50.0
    assert _net(db, "FFF") == _raw_signed_net(db, "FFF")


def test_round_trip_then_rebuy_holds_the_new_lot(db):
    # Trade a symbol, close it, buy it again → only the new lot is held.
    _seed(db, [
        ("GGG", "buy", 100, 2.0, "closed"),
        ("GGG", "sell", 100, 2.1, "closed"),
        ("GGG", "buy", 50, 2.2, "open"),
    ])
    assert _net(db, "GGG") == 50.0


def test_two_round_trips_then_held_long(db):
    # Multiple completed round-trips plus a current hold — none of the
    # closed buys may linger as a phantom long.
    _seed(db, [
        ("HHH", "buy", 100, 1.0, "closed"),
        ("HHH", "sell", 100, 1.1, "closed"),
        ("HHH", "buy", 200, 1.2, "closed"),
        ("HHH", "sell", 200, 1.3, "closed"),
        ("HHH", "buy", 30, 1.4, "open"),
    ])
    assert _net(db, "HHH") == 30.0
