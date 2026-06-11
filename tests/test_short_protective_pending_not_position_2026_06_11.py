"""Short-side protective placeholders must not become phantom longs
(2026-06-11).

The bug: get_virtual_positions' entry-side status filter excluded
'pending_protective' only on the EXIT side (sell/cover) — written
when every protective was sell-side (protecting longs). Protectives
for SHORT positions are BUY orders; their pending placeholder rows
counted as real long entry lots in the FIFO.

Caught live on p93: a 1,065-share NU short @ $11.61 with a
protective stop (buy 1,065 @ $12.82) and TP (buy 1,065 @ $10.80)
rendered as a LONG of +1,065 @ $11.81 — flipping a ~$12.4K
liability into a ~$12.6K asset and inflating the dashboard P&L by
~$25K (+$22K "profit" on a profile that was actually ~$3K down).

Cash math was never affected (get_virtual_account_info excludes
placeholders on every side); only the position book leaked.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "p.db")
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "CREATE TABLE trades ("
            " id INTEGER PRIMARY KEY, timestamp TEXT, symbol TEXT,"
            " side TEXT, qty REAL, price REAL, order_id TEXT,"
            " signal_type TEXT, status TEXT, reason TEXT,"
            " occ_symbol TEXT, stop_loss REAL, take_profit REAL)")
        conn.commit()
    return path


def _insert(path, ts, symbol, side, qty, price, status,
            signal_type="", occ=None):
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price,"
            " status, signal_type, occ_symbol)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, symbol, side, qty, price, status, signal_type, occ),
        )
        conn.commit()


def test_nu_short_regression_protective_pendings_ignored(db):
    """The exact p93 shape: open short + two BUY-side protective
    placeholders. The book must show the SHORT, not a phantom long."""
    from journal import get_virtual_positions
    _insert(db, "2026-06-11T13:54:00", "NU", "short", 1065, 11.61,
            "open", "STRONG_SELL")
    _insert(db, "2026-06-11T14:00:00", "NU", "buy", 1065, 12.82,
            "pending_protective", "PROTECTIVE_STOP")
    _insert(db, "2026-06-11T14:00:00", "NU", "buy", 1065, 10.80,
            "pending_protective", "PROTECTIVE_TP")
    pos = get_virtual_positions(db, price_fetcher=lambda s: 11.70)
    nu = [p for p in pos if p["symbol"] == "NU"]
    assert len(nu) == 1, f"expected one NU position, got {nu}"
    assert float(nu[0]["qty"]) == -1065, (
        f"NU must be a SHORT of -1065; got qty={nu[0]['qty']} — "
        "protective BUY placeholders are leaking into the FIFO as "
        "long entry lots (the +$22K phantom-P&L bug)."
    )
    assert abs(float(nu[0]["avg_entry_price"]) - 11.61) < 0.001


def test_long_protective_pendings_still_ignored(db):
    """Symmetric case that always worked: a long with SELL-side
    placeholders must show the plain long."""
    from journal import get_virtual_positions
    _insert(db, "2026-06-11T14:02:00", "MARA", "buy", 940, 13.025,
            "open", "BUY")
    _insert(db, "2026-06-11T14:02:00", "MARA", "sell", 940, 12.1132,
            "pending_protective", "PROTECTIVE_STOP")
    _insert(db, "2026-06-11T14:02:00", "MARA", "sell", 940, 14.588,
            "pending_protective", "PROTECTIVE_TP")
    pos = get_virtual_positions(db, price_fetcher=lambda s: 13.10)
    mara = [p for p in pos if p["symbol"] == "MARA"]
    assert len(mara) == 1
    assert float(mara[0]["qty"]) == 940


def test_filled_protective_still_closes_the_lot(db):
    """When a protective FILLS, the reconciler flips its row to
    'closed' — and a closed SELL must still consume the open BUY lot
    (the partial-close contract). Pin that the new entry-side
    exclusion didn't break the exit-side keep-closed rule."""
    from journal import get_virtual_positions
    _insert(db, "2026-06-10T17:17:00", "WCT", "buy", 10158, 2.215,
            "open", "BUY")
    _insert(db, "2026-06-10T17:28:00", "WCT", "sell", 10158, 2.07,
            "closed", "PROTECTIVE_FILL")
    pos = get_virtual_positions(db, price_fetcher=lambda s: 2.10)
    assert not [p for p in pos if p["symbol"] == "WCT"], (
        "Filled protective SELL (status=closed) must consume the "
        "BUY lot — WCT should be flat."
    )
