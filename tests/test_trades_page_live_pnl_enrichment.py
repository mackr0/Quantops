"""Pin the /trades page's live-P&L enrichment behavior.

Caught 2026-05-10: the trades page was a "clean order log" — it
returned raw journal rows with no Alpaca enrichment. That meant:
  - SELL legs of multileg OPENS rendered blank P&L (pnl is NULL
    until the multileg unwinds)
  - BUY rows of currently-open positions rendered blank P&L
    (realized P&L only lives on the closing trade)
The dashboard worked because `_enriched_positions` injected
`unrealized_pl` from Alpaca per-position. The /trades page didn't
do this, so users saw `--` on every open position row.

Plus a found-along-the-way: `_get_trade_history_for_profile`
swallowed every read failure with `except Exception: return []`,
which Issue 9 cleaned up everywhere else in views.py but missed.

This test pins:
1. `_enrich_trade_history_with_live_pnl` attaches unrealized_pl
   from Alpaca to journal rows whose OCC/symbol matches an open
   position — for both option legs and stock rows.
2. Closed trades (no matching open position) are NOT enriched.
3. Only the most recent journal row per position key is enriched
   (so repeated adds-to-position don't each show duplicate
   position-level unrealized P&L).
4. `_get_trade_history_for_profile` logs a warning naming the
   profile when DB read fails, instead of silently returning [].
"""

import logging
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


class TestEnrichTradeHistoryWithLivePnL:
    def test_attaches_unrealized_pnl_to_open_option_legs(self):
        """SELL leg + BUY leg of an open multileg both get unrealized_pl
        from their respective Alpaca positions, matched by OCC."""
        from views import _enrich_trade_history_with_live_pnl

        trades = [
            {"symbol": "BKLN", "occ_symbol": "BKLN260612P00019500",
             "side": "sell", "qty": 2, "price": 0.50, "pnl": None,
             "timestamp": "2026-05-10 03:21"},
            {"symbol": "BKLN", "occ_symbol": "BKLN260612P00018500",
             "side": "buy", "qty": 2, "price": 0.75, "pnl": None,
             "timestamp": "2026-05-10 03:21"},
        ]
        fake_positions = [
            {"symbol": "BKLN", "occ_symbol": "BKLN260612P00019500",
             "qty": -2, "avg_entry_price": 0.50, "current_price": 0.40,
             "unrealized_pl": 20.0, "unrealized_plpc": 0.20,
             "market_value": -80.0},
            {"symbol": "BKLN", "occ_symbol": "BKLN260612P00018500",
             "qty": 2, "avg_entry_price": 0.75, "current_price": 0.85,
             "unrealized_pl": 20.0, "unrealized_plpc": 0.13,
             "market_value": 170.0},
        ]

        ctx = MagicMock()
        with patch("views._safe_positions", return_value=fake_positions):
            _enrich_trade_history_with_live_pnl(trades, ctx)

        assert trades[0]["unrealized_pl"] == 20.0
        assert trades[0]["unrealized_plpc"] == 0.20
        assert trades[0]["current_price"] == 0.40
        assert trades[1]["unrealized_pl"] == 20.0
        assert trades[1]["current_price"] == 0.85

    def test_attaches_unrealized_pnl_to_open_stock_position(self):
        """Stock BUY of an open position (no occ_symbol) is matched by
        symbol and gets enriched."""
        from views import _enrich_trade_history_with_live_pnl

        trades = [
            {"symbol": "AAPL", "occ_symbol": None,
             "side": "buy", "qty": 100, "price": 150.0, "pnl": None,
             "timestamp": "2026-05-10 02:00"},
        ]
        fake_positions = [
            {"symbol": "AAPL", "occ_symbol": None,
             "qty": 100, "avg_entry_price": 150.0, "current_price": 152.0,
             "unrealized_pl": 200.0, "unrealized_plpc": 0.013,
             "market_value": 15200.0},
        ]

        ctx = MagicMock()
        with patch("views._safe_positions", return_value=fake_positions):
            _enrich_trade_history_with_live_pnl(trades, ctx)

        assert trades[0]["unrealized_pl"] == 200.0
        assert trades[0]["current_price"] == 152.0

    def test_closed_trades_are_not_enriched(self):
        """A SELL row that already has realized pnl AND whose symbol is
        not in the open position list must NOT be touched (the macro's
        realized-pnl branch handles it)."""
        from views import _enrich_trade_history_with_live_pnl

        trades = [
            {"symbol": "MSFT", "occ_symbol": None,
             "side": "sell", "qty": 50, "price": 400.0, "pnl": 250.0,
             "timestamp": "2026-05-09 14:30"},
        ]
        # No live MSFT position
        fake_positions = []

        ctx = MagicMock()
        with patch("views._safe_positions", return_value=fake_positions):
            _enrich_trade_history_with_live_pnl(trades, ctx)

        assert "unrealized_pl" not in trades[0]
        assert trades[0]["pnl"] == 250.0  # realized pnl untouched

    def test_only_most_recent_row_per_key_gets_enriched(self):
        """If three BUY rows averaged into the same AAPL position, only
        the most recent (first in DESC order) gets unrealized_pl —
        otherwise the user would see the same $200 unrealized 3 times
        and think they've made $600."""
        from views import _enrich_trade_history_with_live_pnl

        trades = [
            {"symbol": "AAPL", "occ_symbol": None, "side": "buy",
             "qty": 50, "price": 152.0, "pnl": None,
             "timestamp": "2026-05-10 14:00"},
            {"symbol": "AAPL", "occ_symbol": None, "side": "buy",
             "qty": 30, "price": 151.0, "pnl": None,
             "timestamp": "2026-05-10 13:00"},
            {"symbol": "AAPL", "occ_symbol": None, "side": "buy",
             "qty": 20, "price": 150.0, "pnl": None,
             "timestamp": "2026-05-10 12:00"},
        ]
        fake_positions = [
            {"symbol": "AAPL", "occ_symbol": None,
             "qty": 100, "avg_entry_price": 151.0, "current_price": 153.0,
             "unrealized_pl": 200.0, "unrealized_plpc": 0.013,
             "market_value": 15300.0},
        ]

        ctx = MagicMock()
        with patch("views._safe_positions", return_value=fake_positions):
            _enrich_trade_history_with_live_pnl(trades, ctx)

        # Most recent enriched
        assert trades[0]["unrealized_pl"] == 200.0
        # Older rows untouched (would double/triple-count in the UI)
        assert "unrealized_pl" not in trades[1]
        assert "unrealized_pl" not in trades[2]

    def test_empty_inputs_no_op(self):
        """Empty trades list and empty positions list both no-op safely."""
        from views import _enrich_trade_history_with_live_pnl

        ctx = MagicMock()
        # Empty trades — does not even call _safe_positions
        _enrich_trade_history_with_live_pnl([], ctx)

        # Empty positions — no rows mutated
        trades = [{"symbol": "AAPL", "occ_symbol": None, "side": "buy",
                   "qty": 100, "price": 150.0, "pnl": None,
                   "timestamp": "2026-05-10 02:00"}]
        with patch("views._safe_positions", return_value=[]):
            _enrich_trade_history_with_live_pnl(trades, ctx)
        assert "unrealized_pl" not in trades[0]


class TestGetTradeHistoryFailureSurfacing:
    def test_db_read_failure_logs_warning_naming_profile(self, caplog):
        """When the profile DB can't be opened, the function logs a
        warning naming the profile and the underlying error, instead
        of swallowing it. Caught 2026-05-10: original code had
        `except Exception: return []` which made every read failure
        invisible — same shape Issue 9 spent the day cleaning up."""
        from views import _get_trade_history_for_profile

        with patch("views.open_profile_db",
                   side_effect=RuntimeError("DB locked")):
            with caplog.at_level(logging.WARNING, logger="views"):
                result = _get_trade_history_for_profile(profile_id=999)

        assert result == []  # graceful degrade — page still renders
        # But the failure is now observable.
        matching = [r for r in caplog.records
                    if "999" in r.getMessage()
                    and "DB locked" in r.getMessage()]
        assert matching, (
            "Expected a warning naming profile 999 + the DB error. Got: "
            f"{[r.getMessage() for r in caplog.records]}"
        )
