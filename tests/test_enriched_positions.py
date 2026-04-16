"""Tests for `views._enriched_positions` (2026-04-15).

Dashboard now shows open positions in the same expandable format as
`/trades`, enriched with AI reasoning / confidence / stop / target
from the profile's journal DB. This module tests the merge logic.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def tmp_profile_db(monkeypatch):
    """Create a throwaway quantopsai_profile_<N>.db with a trades table."""
    tmpdir = tempfile.mkdtemp()
    monkeypatch.chdir(tmpdir)
    profile_id = 999
    db_path = f"quantopsai_profile_{profile_id}.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            qty REAL NOT NULL,
            price REAL,
            ai_reasoning TEXT,
            ai_confidence REAL,
            stop_loss REAL,
            take_profit REAL,
            status TEXT DEFAULT 'open',
            pnl REAL,
            decision_price REAL,
            fill_price REAL,
            slippage_pct REAL,
            reason TEXT
        )
    """)
    conn.commit()
    conn.close()
    yield profile_id, db_path


def _ctx_with_positions(positions):
    api = MagicMock()
    api.list_positions.return_value = [
        SimpleNamespace(
            symbol=p["symbol"], qty=str(p["qty"]),
            market_value=str(p.get("market_value", 1000)),
            unrealized_pl=str(p.get("unrealized_pl", 10)),
            unrealized_plpc=str(p.get("unrealized_plpc", 0.01)),
            current_price=str(p.get("current_price", 100)),
            avg_entry_price=str(p.get("avg_entry_price", 99)),
        )
        for p in positions
    ]
    return SimpleNamespace(
        get_alpaca_api=lambda: api,
        display_name="Test", segment="small",
    )


class TestEnrichedPositions:
    def test_positions_gain_ai_metadata_from_trades_table(self, tmp_profile_db):
        from views import _enriched_positions
        profile_id, db_path = tmp_profile_db

        # Insert a matching trade with rich AI metadata
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, "
            "ai_reasoning, ai_confidence, stop_loss, take_profit, "
            "decision_price, fill_price, slippage_pct) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("2026-04-15T10:00:00", "AAPL", "buy", 5, 180.0,
             "Strong setup with volume", 72, 175.0, 190.0, 180.0, 180.25, 0.014),
        )
        conn.commit()
        conn.close()

        ctx = _ctx_with_positions([{"symbol": "AAPL", "qty": 5}])
        out = _enriched_positions(ctx, profile_id)
        assert len(out) == 1
        row = out[0]
        assert row["symbol"] == "AAPL"
        assert row["ai_reasoning"] == "Strong setup with volume"
        assert row["ai_confidence"] == 72
        assert row["stop_loss"] == 175.0
        assert row["take_profit"] == 190.0
        assert row["slippage_pct"] == 0.014
        # Open position → no realized pnl, unrealized fields from Alpaca
        assert row["pnl"] is None
        assert row["unrealized_pl"] == 10
        assert row["current_price"] == 100

    def test_most_recent_trade_wins_when_multiple(self, tmp_profile_db):
        """If we rebought the same symbol after closing once, the macro
        should get the CURRENT open trade's metadata, not the old one."""
        from views import _enriched_positions
        profile_id, db_path = tmp_profile_db

        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, ai_reasoning, ai_confidence) "
            "VALUES (?,?,?,?,?,?)",
            ("2026-04-10T10:00:00", "AAPL", "buy", 3, "OLD reasoning", 50),
        )
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, ai_reasoning, ai_confidence) "
            "VALUES (?,?,?,?,?,?)",
            ("2026-04-15T10:00:00", "AAPL", "buy", 5, "NEW reasoning", 80),
        )
        conn.commit()
        conn.close()

        ctx = _ctx_with_positions([{"symbol": "AAPL", "qty": 5}])
        out = _enriched_positions(ctx, profile_id)
        assert out[0]["ai_reasoning"] == "NEW reasoning"
        assert out[0]["ai_confidence"] == 80

    def test_position_without_matching_trade_row_still_renders(self, tmp_profile_db):
        """Manual Alpaca trades (not executed by our pipeline) won't have
        AI metadata. The position should still render with None fields."""
        from views import _enriched_positions
        profile_id, _ = tmp_profile_db

        ctx = _ctx_with_positions([{"symbol": "AAPL", "qty": 5}])
        out = _enriched_positions(ctx, profile_id)
        assert len(out) == 1
        row = out[0]
        assert row["symbol"] == "AAPL"
        assert row["ai_reasoning"] is None
        assert row["ai_confidence"] is None
        # Critical: unrealized_pl and current_price still present so the
        # macro's open-position P&L column works
        assert row["unrealized_pl"] == 10
        assert row["current_price"] == 100

    def test_missing_db_does_not_crash(self, monkeypatch):
        """No profile DB → positions return Alpaca-only shape, not crash."""
        from views import _enriched_positions
        import tempfile
        monkeypatch.chdir(tempfile.mkdtemp())

        ctx = _ctx_with_positions([{"symbol": "AAPL", "qty": 5}])
        out = _enriched_positions(ctx, 999)
        assert len(out) == 1
        assert out[0]["symbol"] == "AAPL"
        assert out[0]["ai_reasoning"] is None

    def test_short_position_gets_sell_side(self, tmp_profile_db):
        """Alpaca returns negative qty for shorts — side should be 'sell'."""
        from views import _enriched_positions
        profile_id, _ = tmp_profile_db

        ctx = _ctx_with_positions([{"symbol": "AAPL", "qty": -3}])
        out = _enriched_positions(ctx, profile_id)
        assert out[0]["side"] == "sell"
        assert out[0]["qty"] == 3  # abs value for display

    def test_empty_positions_returns_empty_list(self, tmp_profile_db):
        from views import _enriched_positions
        profile_id, _ = tmp_profile_db

        ctx = _ctx_with_positions([])
        out = _enriched_positions(ctx, profile_id)
        assert out == []
