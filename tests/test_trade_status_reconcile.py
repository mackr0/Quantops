"""Tests for `journal.reconcile_trade_statuses` (2026-04-15).

Background: `trader.check_exits` used to log exit SELL rows without
`status="closed"` (trade_pipeline.py did; trader.py didn't). BUY rows
were never updated when positions closed. Result: the trades page
showed closed positions as still "open" — confusing UX.

The reconcile function fixes drift between `trades.status` and reality.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest


@pytest.fixture
def fresh_db(monkeypatch):
    tmpdir = tempfile.mkdtemp()
    monkeypatch.chdir(tmpdir)
    path = os.path.join(tmpdir, "journal_test.db")
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT DEFAULT (datetime('now')),
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            qty REAL NOT NULL,
            price REAL,
            pnl REAL,
            status TEXT DEFAULT 'open'
        )
    """)
    conn.commit()
    conn.close()
    return path


def _insert(db_path, symbol, side, qty=10, price=None, pnl=None,
            status="open", timestamp=None):
    conn = sqlite3.connect(db_path)
    if timestamp:
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, pnl, status) "
            "VALUES (?,?,?,?,?,?,?)",
            (timestamp, symbol, side, qty, price, pnl, status),
        )
    else:
        conn.execute(
            "INSERT INTO trades (symbol, side, qty, price, pnl, status) "
            "VALUES (?,?,?,?,?,?)",
            (symbol, side, qty, price, pnl, status),
        )
    conn.commit()
    conn.close()


def _get_pnl(db_path, symbol, side):
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT pnl FROM trades WHERE symbol=? AND side=? ORDER BY id",
        (symbol, side),
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def _status(db_path, symbol, side):
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT status FROM trades WHERE symbol=? AND side=?",
        (symbol, side),
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


class TestFixSellRows:
    def test_sells_with_pnl_but_open_status_get_closed(self, fresh_db):
        from journal import reconcile_trade_statuses
        _insert(fresh_db, "AAPL", "sell", pnl=5.50, status="open")
        _insert(fresh_db, "TSLA", "sell", pnl=-3.20, status="open")

        result = reconcile_trade_statuses(db_path=fresh_db)
        assert result["sells_fixed"] == 2
        assert _status(fresh_db, "AAPL", "sell") == ["closed"]
        assert _status(fresh_db, "TSLA", "sell") == ["closed"]

    def test_sell_without_pnl_stays_open(self, fresh_db):
        """Exits without realized pnl can't be confirmed closed — leave alone."""
        from journal import reconcile_trade_statuses
        _insert(fresh_db, "AAPL", "sell", pnl=None, status="open")
        reconcile_trade_statuses(db_path=fresh_db)
        assert _status(fresh_db, "AAPL", "sell") == ["open"]

    def test_already_closed_sells_unchanged(self, fresh_db):
        from journal import reconcile_trade_statuses
        _insert(fresh_db, "AAPL", "sell", pnl=5.50, status="closed")
        result = reconcile_trade_statuses(db_path=fresh_db)
        assert result["sells_fixed"] == 0


class TestFixBuyRowsWithLivePositions:
    def test_buys_not_in_broker_list_left_alone_race_safe(self, fresh_db):
        """Step 2 of reconcile_trade_statuses was REMOVED 2026-05-18
        after the race-condition variant caused a second outage. The
        prior behavior closed any BUY whose symbol wasn't in the
        broker's current positions — but between submit and fill, the
        broker hasn't registered fresh orders yet, so the SQL flipped
        valid open BUYs to closed and collapsed dashboard equity.
        After the fix, this path is a no-op; the per-trade reasoning
        in reconcile_journal_to_broker._classify_long_phantom handles
        legitimate closes by checking each BUY's order_id status."""
        from journal import reconcile_trade_statuses
        _insert(fresh_db, "AAPL", "buy", status="open")
        _insert(fresh_db, "TSLA", "buy", status="open")
        _insert(fresh_db, "HIMS", "buy", status="open")

        # Only AAPL shows in broker — but per the new design, that
        # alone is NOT enough to close TSLA/HIMS BUYs. The race
        # window between submit and broker registration means the
        # broker reply may be incomplete.
        result = reconcile_trade_statuses(
            db_path=fresh_db, open_symbols={"AAPL"},
        )
        assert result["buys_fixed"] == 0
        assert _status(fresh_db, "AAPL", "buy") == ["open"]
        assert _status(fresh_db, "TSLA", "buy") == ["open"]
        assert _status(fresh_db, "HIMS", "buy") == ["open"]

    def test_empty_open_symbols_leaves_buys_alone(self, fresh_db):
        """Empty broker response is AMBIGUOUS (could be a real zero OR
        a transient broker failure that returned empty). Closing every
        BUY on the ambiguous case hides real positions — this exact
        bug fired 2026-05-18 13:30 ET and collapsed dashboard equity
        from $3M to $2.27M within minutes of market open. After the
        fix, open_symbols=set() leaves BUYs alone; the FIFO matching
        in step 3 still closes BUYs that have a matching SELL with
        realized pnl, which is the correct close-detection path that
        doesn't depend on broker availability."""
        from journal import reconcile_trade_statuses
        _insert(fresh_db, "AAPL", "buy", status="open")
        _insert(fresh_db, "TSLA", "buy", status="open")
        result = reconcile_trade_statuses(db_path=fresh_db, open_symbols=set())
        assert result["buys_fixed"] == 0
        assert _status(fresh_db, "AAPL", "buy") == ["open"]
        assert _status(fresh_db, "TSLA", "buy") == ["open"]

    def test_active_positions_preserved(self, fresh_db):
        from journal import reconcile_trade_statuses
        _insert(fresh_db, "AAPL", "buy", status="open")
        reconcile_trade_statuses(db_path=fresh_db, open_symbols={"AAPL"})
        assert _status(fresh_db, "AAPL", "buy") == ["open"]


class TestFixBuyRowsHeuristic:
    """The "sell-implied-close heuristic" (open_symbols=None branch)
    was removed 2026-05-18 alongside the broker-open-symbols branch:
    both shared the same partial-sell-closes-everything pattern.
    A single SELL row with pnl shouldn't close every open BUY for that
    symbol — there may be 100 shares left after a 10-share partial
    sell. The correct close path is reconcile_journal_to_broker._classify_long_phantom
    which checks each BUY's order_id status individually."""

    def test_buy_with_matching_sell_stays_open(self, fresh_db):
        """A SELL with pnl no longer triggers a blanket BUY close.
        FIFO consumption is handled by get_virtual_positions at read
        time; status flips happen only via _classify_long_phantom or
        explicit per-id UPDATEs."""
        from journal import reconcile_trade_statuses
        _insert(fresh_db, "AAPL", "buy", status="open")
        _insert(fresh_db, "AAPL", "sell", pnl=5.50, status="closed")
        reconcile_trade_statuses(db_path=fresh_db)  # no open_symbols arg
        assert _status(fresh_db, "AAPL", "buy") == ["open"]

    def test_buy_without_matching_sell_stays_open(self, fresh_db):
        from journal import reconcile_trade_statuses
        _insert(fresh_db, "AAPL", "buy", status="open")
        reconcile_trade_statuses(db_path=fresh_db)
        assert _status(fresh_db, "AAPL", "buy") == ["open"]


class TestReturnedCounts:
    def test_zero_updates_returns_zero(self, fresh_db):
        from journal import reconcile_trade_statuses
        result = reconcile_trade_statuses(db_path=fresh_db, open_symbols=set())
        assert result["sells_fixed"] == 0
        assert result["buys_fixed"] == 0
        assert result["pnl_computed"] == 0

    def test_both_fixes_reported_independently(self, fresh_db):
        """Step 1 (close SELLs with realized pnl) fires independently
        of step 2. With open_symbols=set() step 2 is a no-op (post
        2026-05-18 fix), so only sells_fixed is 1; buys_fixed is 0."""
        from journal import reconcile_trade_statuses
        _insert(fresh_db, "AAPL", "sell", pnl=5.0, status="open")
        _insert(fresh_db, "TSLA", "buy", status="open")
        result = reconcile_trade_statuses(db_path=fresh_db, open_symbols=set())
        assert result["sells_fixed"] == 1
        assert result["buys_fixed"] == 0


# ---------------------------------------------------------------------------
# FIFO pnl attribution on BUY rows
# ---------------------------------------------------------------------------

class TestBuyRowsPnlNotBackfilled:
    """BUY rows no longer get pnl backfilled — realized P&L belongs on
    the SELL row only. The UI has separate Unrealized/Realized columns."""

    def test_closed_buy_stays_null_pnl(self, fresh_db):
        from journal import reconcile_trade_statuses
        _insert(fresh_db, "AAPL", "buy", qty=10, price=100,
                timestamp="2026-04-15T10:00:00")
        _insert(fresh_db, "AAPL", "sell", qty=10, price=110, pnl=100.0,
                timestamp="2026-04-15T14:00:00", status="closed")
        reconcile_trade_statuses(db_path=fresh_db, open_symbols=set())
        assert _get_pnl(fresh_db, "AAPL", "buy") == [None]

    def test_open_buy_stays_null_pnl(self, fresh_db):
        from journal import reconcile_trade_statuses
        _insert(fresh_db, "AAPL", "buy", qty=10, price=100,
                timestamp="2026-04-15T10:00:00")
        reconcile_trade_statuses(db_path=fresh_db, open_symbols={"AAPL"})
        assert _get_pnl(fresh_db, "AAPL", "buy") == [None]
