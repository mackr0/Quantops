"""Tests for `journal.get_virtual_account_info` — virtual equity tracker.

Phase 2 of the Virtual Account Layer.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest


@pytest.fixture
def vdb(monkeypatch):
    tmpdir = tempfile.mkdtemp()
    monkeypatch.chdir(tmpdir)
    path = os.path.join(tmpdir, "virt.db")
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


def _trade(db, symbol, side, qty, price, ts="2026-04-15T10:00:00"):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO trades (timestamp, symbol, side, qty, price) VALUES (?,?,?,?,?)",
        (ts, symbol, side, qty, price),
    )
    conn.commit()
    conn.close()


class TestCashTracking:
    def test_initial_capital_no_trades(self, vdb):
        from journal import get_virtual_account_info
        info = get_virtual_account_info(db_path=vdb, initial_capital=10000)
        assert info["cash"] == 10000
        assert info["equity"] == 10000
        assert info["buying_power"] == 10000
        assert info["portfolio_value"] == 0

    def test_buy_reduces_cash(self, vdb):
        from journal import get_virtual_account_info
        _trade(vdb, "AAPL", "buy", 10, 100)  # cost = $1000
        info = get_virtual_account_info(db_path=vdb, initial_capital=10000)
        assert info["cash"] == 9000  # 10000 - 1000
        assert info["portfolio_value"] == 1000  # 10 * 100 (no price fetcher)
        assert info["equity"] == 10000  # cash + portfolio = unchanged

    def test_sell_increases_cash(self, vdb):
        from journal import get_virtual_account_info
        _trade(vdb, "AAPL", "buy", 10, 100, ts="2026-04-15T10:00:00")
        _trade(vdb, "AAPL", "sell", 10, 110, ts="2026-04-15T14:00:00")
        info = get_virtual_account_info(db_path=vdb, initial_capital=10000)
        assert info["cash"] == 10100  # 10000 - 1000 + 1100
        assert info["portfolio_value"] == 0
        assert info["equity"] == 10100

    def test_profitable_round_trip(self, vdb):
        from journal import get_virtual_account_info
        _trade(vdb, "AAPL", "buy", 10, 100, ts="2026-04-15T10:00:00")
        _trade(vdb, "AAPL", "sell", 10, 120, ts="2026-04-15T14:00:00")
        info = get_virtual_account_info(db_path=vdb, initial_capital=10000)
        # Profit = (120-100)*10 = $200
        assert info["equity"] == 10200
        assert info["cash"] == 10200

    def test_losing_round_trip(self, vdb):
        from journal import get_virtual_account_info
        _trade(vdb, "AAPL", "buy", 10, 100, ts="2026-04-15T10:00:00")
        _trade(vdb, "AAPL", "sell", 10, 90, ts="2026-04-15T14:00:00")
        info = get_virtual_account_info(db_path=vdb, initial_capital=10000)
        assert info["equity"] == 9900
        assert info["cash"] == 9900


class TestWithPriceFetcher:
    def test_unrealized_gain_reflected_in_equity(self, vdb):
        from journal import get_virtual_account_info
        _trade(vdb, "AAPL", "buy", 10, 100)
        info = get_virtual_account_info(
            db_path=vdb, initial_capital=10000,
            price_fetcher=lambda sym: 120.0,
        )
        assert info["cash"] == 9000
        assert info["portfolio_value"] == 1200  # 10 * 120
        assert info["equity"] == 10200  # 9000 + 1200

    def test_unrealized_loss_reflected_in_equity(self, vdb):
        from journal import get_virtual_account_info
        _trade(vdb, "AAPL", "buy", 10, 100)
        info = get_virtual_account_info(
            db_path=vdb, initial_capital=10000,
            price_fetcher=lambda sym: 80.0,
        )
        assert info["cash"] == 9000
        assert info["portfolio_value"] == 800
        assert info["equity"] == 9800


class TestMultiplePositions:
    def test_two_open_positions(self, vdb):
        from journal import get_virtual_account_info
        _trade(vdb, "AAPL", "buy", 10, 100)   # cost $1000
        _trade(vdb, "TSLA", "buy", 5, 200)    # cost $1000
        info = get_virtual_account_info(db_path=vdb, initial_capital=10000)
        assert info["cash"] == 8000       # 10000 - 1000 - 1000
        assert info["portfolio_value"] == 2000
        assert info["equity"] == 10000    # no price change = flat


class TestOutputShape:
    def test_matches_client_shape(self, vdb):
        from journal import get_virtual_account_info
        info = get_virtual_account_info(db_path=vdb, initial_capital=10000)
        required_keys = {"equity", "buying_power", "cash", "portfolio_value", "status"}
        assert set(info.keys()) == required_keys
        assert info["status"] == "ACTIVE"

    def test_buying_power_never_negative(self, vdb):
        from journal import get_virtual_account_info
        # Spend more than initial (shouldn't happen but defensive)
        _trade(vdb, "AAPL", "buy", 200, 100)  # $20K on $10K account
        info = get_virtual_account_info(db_path=vdb, initial_capital=10000)
        assert info["buying_power"] >= 0


class TestUserContextFields:
    def test_is_virtual_defaults_false(self):
        from user_context import UserContext
        ctx = UserContext(
            user_id=1, segment="small", display_name="t",
            alpaca_api_key="k", alpaca_secret_key="s",
            ai_provider="anthropic", ai_model="claude-haiku-4-5-20251001",
            ai_api_key="k", db_path=":memory:",
        )
        assert ctx.is_virtual is False
        assert ctx.initial_capital == 100000.0
