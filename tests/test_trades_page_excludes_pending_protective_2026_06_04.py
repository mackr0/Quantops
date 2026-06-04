"""The /trades page must not render `pending_protective` rows
(2026-06-04).

Background
----------
`bracket_orders.submit_protective_*` writes a `status='pending_protective'`
trades row at PLACEMENT time so the reconciler can UPDATE it on fill
(no synthesis path). These rows are placeholders — the position is
still open, no cash has moved, no actual exit has occurred. They're
meant to be invisible to the user until the broker fires the stop and
the reconciler flips them to status='closed'.

`journal.get_virtual_positions` and `get_virtual_account_info` already
filter `COALESCE(status, 'open') != 'pending_protective'` so position
and cash math correctly ignore them. The /trades page was the last UI
surface that didn't — caught 2026-06-04 when the user saw every
trailing-stop placement render as a "Long Close" row with a
misleadingly positive P&L (borrowed from the still-open entry via
the live-P&L enricher).

Tests pin:
  1. Pending_protective rows are excluded from
     `_get_trade_history_for_profile` output.
  2. Real trades (open BUYs, closed SELLs, canceled rows) still appear.
  3. Kind filter (stocks/options) still works AFTER the exclusion.
  4. Search filter still works AFTER the exclusion.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture
def profile_db_with_pending(tmp_path, monkeypatch):
    """Profile DB seeded with a real BUY, a real SELL (closed), a
    canceled BUY, and a pending_protective placeholder row."""
    db = tmp_path / "quantopsai_profile_999.db"
    monkeypatch.chdir(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            qty REAL NOT NULL,
            price REAL,
            order_id TEXT,
            signal_type TEXT,
            strategy TEXT,
            reason TEXT,
            status TEXT DEFAULT 'open',
            pnl REAL,
            fill_price REAL,
            occ_symbol TEXT
        )
    """)
    # 1. Real open BUY
    conn.execute(
        "INSERT INTO trades (timestamp, symbol, side, qty, price, "
        "order_id, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("2026-06-04T13:30:00", "AVGO", "buy", 21.0, 410.00,
         "entry-buy", "open"),
    )
    # 2. Real closed SELL
    conn.execute(
        "INSERT INTO trades (timestamp, symbol, side, qty, price, "
        "order_id, status, pnl) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("2026-06-04T15:00:00", "PG", "sell", 332.0, 144.50,
         "real-sell", "closed", 498.0),
    )
    # 3. Canceled BUY (should appear — operator may want to see it)
    conn.execute(
        "INSERT INTO trades (timestamp, symbol, side, qty, price, "
        "order_id, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("2026-06-04T13:35:00", "XYZ", "buy", 10.0, 50.00,
         "canceled-buy", "canceled"),
    )
    # 4. The buggy-rendering pending_protective placeholder (the one
    # the user was seeing as "AVGO Protective Trailing Long Close 21")
    conn.execute(
        "INSERT INTO trades (timestamp, symbol, side, qty, price, "
        "order_id, signal_type, status, reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("2026-06-04T13:30:01", "AVGO", "sell", 21.0, None,
         "trailing-oid", "PROTECTIVE_TRAILING", "pending_protective",
         "broker trailing-stop 5.00%; entry_trade=1"),
    )
    conn.commit()
    conn.close()
    return str(db)


def _run_query(profile_db, kind=None, search=None):
    """Call _get_trade_history_for_profile but patched to read from
    our test DB instead of `/opt/quantopsai/quantopsai_profile_<id>.db`.
    Returns the list of dict rows."""
    from views import _get_trade_history_for_profile
    # The function builds the path as f"quantopsai_profile_{profile_id}.db"
    # relative to CWD. monkeypatched chdir in the fixture already
    # makes the test DB findable at that exact path.
    return _get_trade_history_for_profile(999, limit=100,
                                            kind=kind, search=search)


def test_pending_protective_excluded(profile_db_with_pending):
    """The pending_protective placeholder MUST NOT appear in the
    /trades output. This is the load-bearing assertion: if it fails,
    the user sees protective stops rendered as "Long Close" rows
    with borrowed P&L from the still-open entry."""
    rows = _run_query(profile_db_with_pending)
    statuses = [r.get("status") for r in rows]
    assert "pending_protective" not in statuses, (
        "pending_protective rows must be filtered out of /trades — "
        "they're placement-time placeholders, not real exits. "
        f"Got: {[(r['symbol'], r['side'], r['status']) for r in rows]}"
    )


def test_real_trades_still_appear(profile_db_with_pending):
    """The exclusion must not regress: real BUYs, real SELLs, and
    canceled rows must all still be visible."""
    rows = _run_query(profile_db_with_pending)
    statuses = {r["status"] for r in rows}
    symbols = {r["symbol"] for r in rows}
    assert "open" in statuses, "open BUYs must still appear"
    assert "closed" in statuses, "closed SELLs must still appear"
    assert "canceled" in statuses, "canceled rows must still appear"
    assert {"AVGO", "PG", "XYZ"} <= symbols, (
        f"Real symbols missing: expected AVGO/PG/XYZ, got {symbols}"
    )
    # The AVGO row that remains must be the BUY entry, not the
    # pending_protective placeholder. There must be EXACTLY one AVGO
    # row left.
    avgo_rows = [r for r in rows if r["symbol"] == "AVGO"]
    assert len(avgo_rows) == 1
    assert avgo_rows[0]["side"] == "buy"
    assert avgo_rows[0]["status"] == "open"


def test_kind_filter_still_works(profile_db_with_pending):
    """The stocks kind filter (occ_symbol IS NULL) composes correctly
    with the pending_protective exclusion."""
    rows = _run_query(profile_db_with_pending, kind="stocks")
    assert all(r.get("occ_symbol") is None for r in rows)
    assert "pending_protective" not in {r.get("status") for r in rows}


def test_search_filter_still_works(profile_db_with_pending):
    """Symbol search composes correctly with the exclusion."""
    rows = _run_query(profile_db_with_pending, search="AVGO")
    # Exactly one AVGO row remains: the open BUY (not the placeholder).
    assert len(rows) == 1
    assert rows[0]["symbol"] == "AVGO"
    assert rows[0]["side"] == "buy"
    assert rows[0]["status"] == "open"
