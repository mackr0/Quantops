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
        """Virtual and Alpaca producers must expose the same keys so
        downstream consumers (dashboard, exit logic, etc.) treat
        their outputs interchangeably. Phase 1 of the Position class
        refactor (2026-05-11): both producers now return Position
        objects with the same shim-exposed dict interface, so this
        test compares the producers directly instead of a hardcoded
        list."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        from journal import get_virtual_positions
        from client import get_positions

        _buy(vdb, "AAPL", 10, 100.0)
        v_pos = get_virtual_positions(db_path=vdb)

        # Stock row from the Alpaca path with the same shape
        api = MagicMock()
        api.list_positions.return_value = [SimpleNamespace(
            symbol="AAPL", qty="10", avg_entry_price="100.0",
            current_price="100.0", market_value="1000.0",
            unrealized_pl="0.0", unrealized_plpc="0.0",
        )]
        c_pos = get_positions(api=api)

        assert set(v_pos[0].keys()) == set(c_pos[0].keys())
        # Both must include the structural keys downstream consumers
        # have always read.
        for k in ("symbol", "occ_symbol", "qty", "avg_entry_price",
                  "current_price", "market_value",
                  "unrealized_pl", "unrealized_plpc"):
            assert k in v_pos[0]
            assert k in c_pos[0]
        assert v_pos[0]["occ_symbol"] is None  # stock row

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


def _short(db, symbol, qty, price, ts="2026-04-15T10:00:00"):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO trades (timestamp, symbol, side, qty, price) VALUES (?,?,?,?,?)",
        (ts, symbol, "short", qty, price),
    )
    conn.commit()
    conn.close()


def _cover(db, symbol, qty, price, ts="2026-04-15T14:00:00"):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO trades (timestamp, symbol, side, qty, price) VALUES (?,?,?,?,?)",
        (ts, symbol, "cover", qty, price),
    )
    conn.commit()
    conn.close()


class TestShortPositions:
    """Short positions were silently dropped before (side='short' didn't
    match buy/sell/cover in the FIFO). After this fix, shorts open a
    short_lot, covers consume them, and net is reported with negative qty
    matching Alpaca's sign convention."""

    def test_open_short_appears_with_negative_qty(self, vdb):
        from journal import get_virtual_positions
        _short(vdb, "MSFT", 17, 401.83)
        pos = get_virtual_positions(db_path=vdb)
        assert len(pos) == 1
        assert pos[0]["qty"] == -17
        assert pos[0]["avg_entry_price"] == 401.83

    def test_short_then_full_cover_is_flat(self, vdb):
        from journal import get_virtual_positions
        _short(vdb, "MSFT", 17, 401.83, ts="2026-04-29T10:00:00")
        _cover(vdb, "MSFT", 17, 395.00, ts="2026-04-30T10:00:00")
        pos = get_virtual_positions(db_path=vdb)
        assert pos == []

    def test_short_then_partial_cover_keeps_short(self, vdb):
        from journal import get_virtual_positions
        _short(vdb, "MSFT", 20, 400.0, ts="2026-04-29T10:00:00")
        _cover(vdb, "MSFT", 7, 395.00, ts="2026-04-30T10:00:00")
        pos = get_virtual_positions(db_path=vdb)
        assert len(pos) == 1
        assert pos[0]["qty"] == -13

    def test_short_unrealized_pl_correct_sign(self, vdb):
        """Short profits when price falls below entry."""
        from journal import get_virtual_positions
        _short(vdb, "MSFT", 10, 400.0)
        # Price dropped to 380 — short is up $200
        pos = get_virtual_positions(db_path=vdb, price_fetcher=lambda s: 380.0)
        assert pos[0]["unrealized_pl"] == 200.0

    def test_short_unrealized_pl_when_price_rises(self, vdb):
        """Short loses when price rises."""
        from journal import get_virtual_positions
        _short(vdb, "MSFT", 10, 400.0)
        pos = get_virtual_positions(db_path=vdb, price_fetcher=lambda s: 420.0)
        assert pos[0]["unrealized_pl"] == -200.0

    def test_long_and_short_separate_lots_same_symbol(self, vdb):
        """Pair-trade scenario or rotation: profile holds long AAPL,
        opens short AAPL on a separate signal (rare but should track
        both correctly via independent lot stacks)."""
        from journal import get_virtual_positions
        _buy(vdb, "AAPL", 10, 100.0, ts="2026-04-14T10:00:00")
        _short(vdb, "AAPL", 4, 105.0, ts="2026-04-15T10:00:00")
        pos = get_virtual_positions(db_path=vdb)
        # Net long 6 if implementation nets them
        assert len(pos) == 1
        assert pos[0]["qty"] == 6  # 10 long - 4 short = 6 net long

    def test_short_then_buy_does_not_close_short(self, vdb):
        """A 'buy' on a held short should NOT close the short — that's
        what 'cover' is for. buy opens a fresh long lot."""
        from journal import get_virtual_positions
        _short(vdb, "MSFT", 10, 400.0, ts="2026-04-14T10:00:00")
        _buy(vdb, "MSFT", 10, 405.0, ts="2026-04-15T10:00:00")
        pos = get_virtual_positions(db_path=vdb)
        assert pos == []  # 10 short - 10 long net = 0


class TestCanceledRowsExcluded:
    """Caught 2026-05-06: profile_11's INTC #49 was a limit BUY that
    never filled at the broker. Reconcile correctly marked
    status='canceled' but get_virtual_positions read every row
    regardless of status, so the FIFO had a 28-share BUY with no
    matching SELL — phantom kept showing as +35% open for 12 days.
    The query must filter status='canceled'."""

    def _set_status(self, db, status):
        conn = sqlite3.connect(db)
        conn.execute("UPDATE trades SET status=?", (status,))
        conn.commit()
        conn.close()

    def test_canceled_buy_does_not_appear(self, vdb):
        from journal import get_virtual_positions
        _buy(vdb, "INTC", 28, 80.89)
        self._set_status(vdb, "canceled")
        pos = get_virtual_positions(db_path=vdb)
        assert pos == [], f"phantom canceled BUY leaked through: {pos}"

    def test_canceled_does_not_break_real_position(self, vdb):
        """A real BUY plus a canceled BUY in the same symbol —
        canceled is dropped, real one still shows correctly."""
        from journal import get_virtual_positions
        # Real BUY first
        _buy(vdb, "AAPL", 10, 100.0, ts="2026-04-14T10:00:00")
        # Canceled BUY second
        conn = sqlite3.connect(vdb)
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, status) "
            "VALUES (?, ?, ?, ?, ?, 'canceled')",
            ("2026-04-15T10:00:00", "AAPL", "buy", 5, 110.0),
        )
        conn.commit()
        conn.close()
        pos = get_virtual_positions(db_path=vdb)
        assert len(pos) == 1
        assert pos[0]["qty"] == 10  # only the real BUY counts
        assert pos[0]["avg_entry_price"] == 100.0  # real BUY's price

    def test_status_open_default_still_works(self, vdb):
        """Most rows have status='open' from the default. Make sure
        the status filter doesn't accidentally exclude those."""
        from journal import get_virtual_positions
        _buy(vdb, "AAPL", 10, 100.0)
        # Default status='open' is set by schema
        pos = get_virtual_positions(db_path=vdb)
        assert len(pos) == 1
        assert pos[0]["qty"] == 10

    def test_null_status_still_included(self, vdb):
        """Older rows might have NULL status. Treat as 'open' (the
        COALESCE default), not 'canceled'."""
        from journal import get_virtual_positions
        conn = sqlite3.connect(vdb)
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, status) "
            "VALUES (?, ?, ?, ?, ?, NULL)",
            ("2026-04-14T10:00:00", "AAPL", "buy", 10, 100.0),
        )
        conn.commit()
        conn.close()
        pos = get_virtual_positions(db_path=vdb)
        assert len(pos) == 1
        assert pos[0]["qty"] == 10
