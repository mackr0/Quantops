"""2026-06-16 — a stock OVERSELL must show as a real short in the
virtual book (it is a real short at the broker).

`get_virtual_positions` used to DROP the portion of a stock `sell`
that exceeded the available long (it only formed short lots for
options). That hid genuine broker shorts: the delta-hedge runaway
(p128 JOBY) and the p128 SOUN case where a 3772-share sell oversold a
3672 long by 100 — the 100-share short vanished from the book, so the
journal disagreed with the order-id truth (sum of own order_id fills).

The fix: a stock `sell` remainder becomes a short lot IFF it consumed
some open long first (a true oversell). A `sell` that matched NO open
long is an orphan/closed-round-trip artifact (the entry was excluded
as 'closed') and is still dropped — so completed round-trips never
flash a phantom short.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing
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


def _ins(db, **c):
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "INSERT INTO trades (%s) VALUES (%s)"
            % (", ".join(c), ", ".join(["?"] * len(c))),
            list(c.values()))
        conn.commit()


def _qty(db, sym):
    from journal import get_virtual_positions
    pos = get_virtual_positions(db, price_fetcher=lambda s: 10.0)
    for r in (pos if isinstance(pos, list) else []):
        if r.get("symbol") == sym:
            return r.get("qty", 0)
    return 0


def test_stock_oversell_becomes_short(db):
    """Buy 100, then sell 150 → net −50 (oversold by 50 = real short)."""
    _ins(db, symbol="ZZ", side="buy", qty=100, price=10.0,
         order_id="b1", status="open")
    _ins(db, symbol="ZZ", side="sell", qty=150, price=10.0,
         order_id="s1", status="closed")
    assert _qty(db, "ZZ") == -50, (
        "a stock sell that oversold the long must leave a real short, "
        "not be silently dropped"
    )


def test_p128_soun_scenario(db):
    """The exact p128 SOUN shape: buy 3672 (open) + sell 3772 (closed)
    + a separate short 100 → net −200, matching broker truth."""
    _ins(db, symbol="SOUN", side="buy", qty=3672, price=7.715,
         order_id="cb0fe243", status="open")
    _ins(db, symbol="SOUN", side="sell", qty=3772, price=7.33,
         order_id="5a68f754", status="closed")
    _ins(db, symbol="SOUN", side="short", qty=100, price=7.29,
         order_id="cac89a22", status="open")
    assert _qty(db, "SOUN") == -200


def test_completed_round_trip_is_not_a_phantom_short(db):
    """The safety case: a completed round-trip (buy CLOSED + sell
    CLOSED) must net to 0 — the closed sell matched no open long, so
    it is dropped, NOT turned into a phantom short."""
    _ins(db, symbol="RT", side="buy", qty=100, price=10.0,
         order_id="rb", status="closed")
    _ins(db, symbol="RT", side="sell", qty=100, price=11.0,
         order_id="rs", status="closed")
    assert _qty(db, "RT") == 0, (
        "a completed round-trip must be flat, never a phantom short"
    )


def test_pending_exit_does_not_flash_phantom_short(db):
    """A normal exit pre-closes the entry at submit and writes a
    pending_fill sell. With the entry excluded as 'closed', the sell
    matches no open long → dropped, not a phantom short."""
    _ins(db, symbol="PX", side="buy", qty=100, price=10.0,
         order_id="pb", status="closed")
    _ins(db, symbol="PX", side="sell", qty=100, price=10.5,
         order_id="ps", status="pending_fill")
    assert _qty(db, "PX") == 0


def test_exact_close_leaves_no_short(db):
    """Buy 100, sell exactly 100 → flat, no short remainder."""
    _ins(db, symbol="EX", side="buy", qty=100, price=10.0,
         order_id="eb", status="open")
    _ins(db, symbol="EX", side="sell", qty=100, price=10.0,
         order_id="es", status="closed")
    assert _qty(db, "EX") == 0


def test_normal_partial_sell_still_long(db):
    """Buy 100, sell 40 → still long 60 (no spurious short)."""
    _ins(db, symbol="PS", side="buy", qty=100, price=10.0,
         order_id="pb1", status="open")
    _ins(db, symbol="PS", side="sell", qty=40, price=10.0,
         order_id="ps1", status="closed")
    assert _qty(db, "PS") == 60
