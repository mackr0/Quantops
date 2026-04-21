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
    def test_buys_for_symbols_not_in_open_list_get_closed(self, fresh_db):
        from journal import reconcile_trade_statuses
        _insert(fresh_db, "AAPL", "buy", status="open")
        _insert(fresh_db, "TSLA", "buy", status="open")
        _insert(fresh_db, "HIMS", "buy", status="open")

        # Only AAPL is still held
        result = reconcile_trade_statuses(
            db_path=fresh_db, open_symbols={"AAPL"},
        )
        assert result["buys_fixed"] == 2
        assert _status(fresh_db, "AAPL", "buy") == ["open"]
        assert _status(fresh_db, "TSLA", "buy") == ["closed"]
        assert _status(fresh_db, "HIMS", "buy") == ["closed"]

    def test_empty_open_symbols_means_all_buys_closed(self, fresh_db):
        """No live positions → every open BUY is stale."""
        from journal import reconcile_trade_statuses
        _insert(fresh_db, "AAPL", "buy", status="open")
        _insert(fresh_db, "TSLA", "buy", status="open")
        result = reconcile_trade_statuses(db_path=fresh_db, open_symbols=set())
        assert result["buys_fixed"] == 2
        assert _status(fresh_db, "AAPL", "buy") == ["closed"]

    def test_active_positions_preserved(self, fresh_db):
        from journal import reconcile_trade_statuses
        _insert(fresh_db, "AAPL", "buy", status="open")
        reconcile_trade_statuses(db_path=fresh_db, open_symbols={"AAPL"})
        assert _status(fresh_db, "AAPL", "buy") == ["open"]


class TestFixBuyRowsHeuristic:
    """When no live positions list is provided, fall back to the
    sell-implied-close heuristic."""

    def test_buy_with_matching_sell_gets_closed(self, fresh_db):
        from journal import reconcile_trade_statuses
        _insert(fresh_db, "AAPL", "buy", status="open")
        _insert(fresh_db, "AAPL", "sell", pnl=5.50, status="closed")
        reconcile_trade_statuses(db_path=fresh_db)  # no open_symbols arg
        assert _status(fresh_db, "AAPL", "buy") == ["closed"]

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
        from journal import reconcile_trade_statuses
        _insert(fresh_db, "AAPL", "sell", pnl=5.0, status="open")
        _insert(fresh_db, "TSLA", "buy", status="open")
        result = reconcile_trade_statuses(db_path=fresh_db, open_symbols=set())
        assert result["sells_fixed"] == 1
        assert result["buys_fixed"] == 1


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
