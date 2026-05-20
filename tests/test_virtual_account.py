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
            user_id=1, segment="stocks", display_name="t",
            alpaca_api_key="k", alpaca_secret_key="s",
            ai_provider="anthropic", ai_model="claude-haiku-4-5-20251001",
            ai_api_key="k", db_path=":memory:",
        )
        assert ctx.is_virtual is False
        assert ctx.initial_capital == 100000.0


# --- 2026-05-17 audit: cash logic bugs ---


@pytest.fixture
def vdb_with_occ(monkeypatch):
    """Production-shaped trades schema (has occ_symbol). Required to
    test the option-multiplier path."""
    tmpdir = tempfile.mkdtemp()
    monkeypatch.chdir(tmpdir)
    path = os.path.join(tmpdir, "virt.db")
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT DEFAULT (datetime('now')),
            symbol TEXT NOT NULL,
            occ_symbol TEXT,
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


def _trade_with_occ(db, symbol, side, qty, price, occ=None,
                    ts="2026-04-15T10:00:00"):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO trades (timestamp, symbol, occ_symbol, side, qty, price) "
        "VALUES (?,?,?,?,?,?)",
        (ts, symbol, occ, side, qty, price),
    )
    conn.commit()
    conn.close()


class TestStockShortCashCredit:
    """Stock short (side='short') credits cash with the proceeds —
    pre-2026-05-17 the cash logic didn't match 'short' so opening
    a short left cash unchanged but the position lot was tracked
    as a liability. Equity was understated by the short premium."""

    def test_short_open_credits_cash(self, vdb_with_occ):
        from journal import get_virtual_account_info
        # Short 100 shares at $50 → proceeds $5000
        _trade_with_occ(vdb_with_occ, "AAPL", "short", 100, 50)
        info = get_virtual_account_info(
            db_path=vdb_with_occ, initial_capital=10000,
        )
        assert info["cash"] == 15000, (
            "Stock short proceeds (qty*price = $5000) must credit "
            f"cash; expected $15000 (10000 + 5000), got {info['cash']}"
        )

    def test_short_roundtrip_profit(self, vdb_with_occ):
        from journal import get_virtual_account_info
        # Short 100 AAPL @ $50, then cover (buy back) @ $45 → +$500
        _trade_with_occ(vdb_with_occ, "AAPL", "short", 100, 50,
                        ts="2026-04-15T10:00:00")
        _trade_with_occ(vdb_with_occ, "AAPL", "buy", 100, 45,
                        ts="2026-04-15T14:00:00")
        info = get_virtual_account_info(
            db_path=vdb_with_occ, initial_capital=10000,
        )
        # cash = 10000 + 5000 (short proceeds) - 4500 (cover cost) = 10500
        assert info["cash"] == 10500, (
            f"Short profit roundtrip cash: expected $10500, got {info['cash']}"
        )


class TestOptionContractMultiplier:
    """1 option contract = 100 shares. Cash effect of any option
    trade is `qty * price * 100`, not `qty * price`. Pre-2026-05-17
    every option cash effect was off by 100x — billions in error
    if any large option positions were held."""

    def test_option_buy_subtracts_premium_times_100(self, vdb_with_occ):
        from journal import get_virtual_account_info
        # Buy 1 AAPL call @ $2 premium → cash effect = -$200
        _trade_with_occ(
            vdb_with_occ, "AAPL", "buy", 1, 2,
            occ="AAPL260618C00200000",
        )
        info = get_virtual_account_info(
            db_path=vdb_with_occ, initial_capital=10000,
        )
        assert info["cash"] == 9800, (
            "Option buy must apply contract multiplier 100; "
            f"expected $9800 (10000 - 1*2*100), got {info['cash']}"
        )

    def test_option_sell_to_open_credits_premium_times_100(
        self, vdb_with_occ,
    ):
        from journal import get_virtual_account_info
        # Sell-to-open 1 AAPL put @ $1.50 premium → cash effect = +$150
        _trade_with_occ(
            vdb_with_occ, "AAPL", "sell", 1, 1.50,
            occ="AAPL260618P00150000",
        )
        info = get_virtual_account_info(
            db_path=vdb_with_occ, initial_capital=10000,
        )
        assert info["cash"] == 10150, (
            "Option sell premium × 100; "
            f"expected $10150 (10000 + 1*1.50*100), got {info['cash']}"
        )

    def test_option_roundtrip_with_multiplier(self, vdb_with_occ):
        from journal import get_virtual_account_info
        # Buy 2 calls @ $1.00 → -$200; sell @ $1.50 → +$300; profit $100
        _trade_with_occ(
            vdb_with_occ, "AAPL", "buy", 2, 1.00,
            occ="AAPL260618C00200000", ts="2026-04-15T10:00:00",
        )
        _trade_with_occ(
            vdb_with_occ, "AAPL", "sell", 2, 1.50,
            occ="AAPL260618C00200000", ts="2026-04-15T14:00:00",
        )
        info = get_virtual_account_info(
            db_path=vdb_with_occ, initial_capital=10000,
        )
        assert info["cash"] == 10100, (
            f"Option roundtrip P&L: expected $10100, got {info['cash']}"
        )

    def test_stock_trade_does_not_apply_100x_multiplier(
        self, vdb_with_occ,
    ):
        """Regression guard: stocks (occ_symbol IS NULL) must NOT
        get the option multiplier."""
        from journal import get_virtual_account_info
        _trade_with_occ(vdb_with_occ, "AAPL", "buy", 100, 50)  # stock
        info = get_virtual_account_info(
            db_path=vdb_with_occ, initial_capital=10000,
        )
        # 100 shares × $50 × 1 = $5000 (NOT $500,000)
        assert info["cash"] == 5000
