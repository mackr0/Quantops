"""Tests for `journal.get_virtual_positions` — the internal position ledger.

Phase 1 of the Virtual Account Layer. Computes what a profile holds
purely from its trades table, without calling Alpaca.
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


def _buy(db, symbol, qty, price, ts="2026-04-15T10:00:00"):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO trades (timestamp, symbol, side, qty, price) VALUES (?,?,?,?,?)",
        (ts, symbol, "buy", qty, price),
    )
    conn.commit()
    conn.close()


def _sell(db, symbol, qty, price, ts="2026-04-15T14:00:00"):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO trades (timestamp, symbol, side, qty, price) VALUES (?,?,?,?,?)",
        (ts, symbol, "sell", qty, price),
    )
    conn.commit()
    conn.close()


class TestBasicPositions:
    def test_single_buy_creates_position(self, vdb):
        from journal import get_virtual_positions
        _buy(vdb, "AAPL", 10, 150.0)
        pos = get_virtual_positions(db_path=vdb)
        assert len(pos) == 1
        assert pos[0]["symbol"] == "AAPL"
        assert pos[0]["qty"] == 10
        assert pos[0]["avg_entry_price"] == 150.0

    def test_full_sell_removes_position(self, vdb):
        from journal import get_virtual_positions
        _buy(vdb, "AAPL", 10, 150.0)
        _sell(vdb, "AAPL", 10, 160.0)
        pos = get_virtual_positions(db_path=vdb)
        assert len(pos) == 0

    def test_partial_sell_reduces_qty(self, vdb):
        from journal import get_virtual_positions
        _buy(vdb, "AAPL", 10, 150.0)
        _sell(vdb, "AAPL", 3, 160.0)
        pos = get_virtual_positions(db_path=vdb)
        assert len(pos) == 1
        assert pos[0]["qty"] == 7
        assert pos[0]["avg_entry_price"] == 150.0

    def test_no_trades_returns_empty(self, vdb):
        from journal import get_virtual_positions
        assert get_virtual_positions(db_path=vdb) == []


class TestMultipleSymbols:
    def test_two_symbols_independent(self, vdb):
        from journal import get_virtual_positions
        _buy(vdb, "AAPL", 10, 150.0)
        _buy(vdb, "TSLA", 5, 200.0)
        pos = get_virtual_positions(db_path=vdb)
        assert len(pos) == 2
        symbols = {p["symbol"] for p in pos}
        assert symbols == {"AAPL", "TSLA"}

    def test_sell_one_keeps_other(self, vdb):
        from journal import get_virtual_positions
        _buy(vdb, "AAPL", 10, 150.0)
        _buy(vdb, "TSLA", 5, 200.0)
        _sell(vdb, "AAPL", 10, 160.0)
        pos = get_virtual_positions(db_path=vdb)
        assert len(pos) == 1
        assert pos[0]["symbol"] == "TSLA"


class TestFIFOLotTracking:
    def test_multiple_buys_at_different_prices(self, vdb):
        from journal import get_virtual_positions
        _buy(vdb, "AAPL", 10, 100.0, ts="2026-04-15T10:00:00")
        _buy(vdb, "AAPL", 10, 120.0, ts="2026-04-15T11:00:00")
        pos = get_virtual_positions(db_path=vdb)
        assert pos[0]["qty"] == 20
        assert pos[0]["avg_entry_price"] == 110.0  # (10*100 + 10*120) / 20

    def test_fifo_sell_consumes_oldest_lot_first(self, vdb):
        from journal import get_virtual_positions
        _buy(vdb, "AAPL", 10, 100.0, ts="2026-04-15T10:00:00")
        _buy(vdb, "AAPL", 10, 200.0, ts="2026-04-15T11:00:00")
        _sell(vdb, "AAPL", 10, 150.0, ts="2026-04-15T12:00:00")
        pos = get_virtual_positions(db_path=vdb)
        assert pos[0]["qty"] == 10
        # Only the $200 lot remains (FIFO consumed the $100 lot)
        assert pos[0]["avg_entry_price"] == 200.0

    def test_fifo_partial_lot_consumption(self, vdb):
        from journal import get_virtual_positions
        _buy(vdb, "AAPL", 10, 100.0, ts="2026-04-15T10:00:00")
        _buy(vdb, "AAPL", 10, 200.0, ts="2026-04-15T11:00:00")
        _sell(vdb, "AAPL", 5, 150.0, ts="2026-04-15T12:00:00")
        pos = get_virtual_positions(db_path=vdb)
        assert pos[0]["qty"] == 15
        # 5 remaining from $100 lot + 10 from $200 lot
        expected_avg = (5 * 100 + 10 * 200) / 15
        assert abs(pos[0]["avg_entry_price"] - expected_avg) < 0.01


class TestUnrealizedPnL:
    def test_with_price_fetcher(self, vdb):
        from journal import get_virtual_positions
        _buy(vdb, "AAPL", 10, 100.0)
        pos = get_virtual_positions(
            db_path=vdb,
            price_fetcher=lambda sym: 110.0,
        )
        assert pos[0]["current_price"] == 110.0
        assert pos[0]["unrealized_pl"] == 100.0  # (110-100)*10
        assert abs(pos[0]["unrealized_plpc"] - 0.10) < 0.001
        assert pos[0]["market_value"] == 1100.0

    def test_without_price_fetcher_uses_entry(self, vdb):
        from journal import get_virtual_positions
        _buy(vdb, "AAPL", 10, 100.0)
        pos = get_virtual_positions(db_path=vdb)
        # No price fetcher → current_price = avg_entry (no P&L)
        assert pos[0]["current_price"] == 100.0
        assert pos[0]["unrealized_pl"] == 0.0

    def test_loss_scenario(self, vdb):
        from journal import get_virtual_positions
        _buy(vdb, "AAPL", 10, 100.0)
        pos = get_virtual_positions(
            db_path=vdb,
            price_fetcher=lambda sym: 90.0,
        )
        assert pos[0]["unrealized_pl"] == -100.0
        assert pos[0]["unrealized_plpc"] < 0


class TestOutputShape:
    def test_matches_client_get_positions_shape(self, vdb):
        from journal import get_virtual_positions
        _buy(vdb, "AAPL", 10, 100.0)
        pos = get_virtual_positions(db_path=vdb)
        required_keys = {
            "symbol", "qty", "avg_entry_price", "current_price",
            "market_value", "unrealized_pl", "unrealized_plpc",
        }
        assert set(pos[0].keys()) == required_keys

    def test_price_fetcher_failure_falls_back(self, vdb):
        from journal import get_virtual_positions
        _buy(vdb, "AAPL", 10, 100.0)

        def bad_fetcher(sym):
            raise RuntimeError("API down")

        pos = get_virtual_positions(db_path=vdb, price_fetcher=bad_fetcher)
        assert len(pos) == 1
        assert pos[0]["current_price"] == 100.0  # fallback to entry


class TestRoundTrips:
    def test_buy_sell_buy_again(self, vdb):
        from journal import get_virtual_positions
        _buy(vdb, "AAPL", 10, 100.0, ts="2026-04-14T10:00:00")
        _sell(vdb, "AAPL", 10, 110.0, ts="2026-04-14T14:00:00")
        _buy(vdb, "AAPL", 5, 120.0, ts="2026-04-15T10:00:00")
        pos = get_virtual_positions(db_path=vdb)
        assert len(pos) == 1
        assert pos[0]["qty"] == 5
        assert pos[0]["avg_entry_price"] == 120.0
