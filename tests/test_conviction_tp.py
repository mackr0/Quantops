"""Tests for the conviction take-profit override (2026-04-15).

Purpose: when a long position would normally sell at its fixed TP
threshold but the AI still has high conviction AND the trend is
demonstrably intact, skip the TP and let the trailing stop manage
the exit. Prevents capping runaway winners (the IONQ scenario —
we'd sold at +20% while the name continued to +35%).
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Pure predicate — should_skip_take_profit
# ---------------------------------------------------------------------------

class TestPredicate:
    def test_all_conditions_met_returns_true(self):
        from conviction_tp import should_skip_take_profit
        trend = {"adx": 32, "prev_high": 100, "current_close": 102}
        assert should_skip_take_profit("X", ai_confidence=80,
                                        trend=trend,
                                        min_confidence=70, min_adx=25) is True

    def test_low_confidence_returns_false(self):
        from conviction_tp import should_skip_take_profit
        trend = {"adx": 32, "prev_high": 100, "current_close": 102}
        assert should_skip_take_profit("X", ai_confidence=60,
                                        trend=trend,
                                        min_confidence=70, min_adx=25) is False

    def test_none_confidence_returns_false(self):
        """If we have no AI prediction history for this symbol, defer to
        fixed TP rather than skipping blindly."""
        from conviction_tp import should_skip_take_profit
        trend = {"adx": 32, "prev_high": 100, "current_close": 102}
        assert should_skip_take_profit("X", ai_confidence=None,
                                        trend=trend,
                                        min_confidence=70, min_adx=25) is False

    def test_low_adx_returns_false(self):
        """ADX below threshold = trend not strong enough, take the gain."""
        from conviction_tp import should_skip_take_profit
        trend = {"adx": 18, "prev_high": 100, "current_close": 102}
        assert should_skip_take_profit("X", ai_confidence=80,
                                        trend=trend,
                                        min_confidence=70, min_adx=25) is False

    def test_none_adx_returns_false(self):
        from conviction_tp import should_skip_take_profit
        trend = {"adx": None, "prev_high": 100, "current_close": 102}
        assert should_skip_take_profit("X", ai_confidence=80,
                                        trend=trend,
                                        min_confidence=70, min_adx=25) is False

    def test_price_below_prev_high_returns_false(self):
        """Core safety: even if AI and ADX are strong, if price is already
        rolling over (below yesterday's high), take the gain."""
        from conviction_tp import should_skip_take_profit
        trend = {"adx": 32, "prev_high": 100, "current_close": 98}
        assert should_skip_take_profit("X", ai_confidence=80,
                                        trend=trend,
                                        min_confidence=70, min_adx=25) is False

    def test_missing_trend_returns_false(self):
        """No bars available → can't confirm trend intact → don't skip."""
        from conviction_tp import should_skip_take_profit
        assert should_skip_take_profit("X", ai_confidence=80,
                                        trend=None,
                                        min_confidence=70, min_adx=25) is False


# ---------------------------------------------------------------------------
# Integration with check_stop_loss_take_profit
# ---------------------------------------------------------------------------

class TestIntegrationWithExitLogic:
    def test_skip_fn_prevents_long_take_profit_trigger(self):
        from portfolio_manager import check_stop_loss_take_profit

        positions = [{
            "symbol": "IONQ",
            "current_price": 12.00,
            "avg_entry_price": 10.00,
            "qty": 10,
            "stop_loss": 0.03,
            "take_profit": 0.10,  # +10% TP
        }]
        # +20% — would normally trigger TP; skip fn returns True
        triggered = check_stop_loss_take_profit(
            positions, stop_loss_pct=0.03, take_profit_pct=0.10,
            conviction_tp_skip=lambda sym, pct: True,
        )
        assert triggered == []

    def test_skip_fn_returning_false_still_triggers_tp(self):
        from portfolio_manager import check_stop_loss_take_profit

        positions = [{
            "symbol": "IONQ",
            "current_price": 12.00,
            "avg_entry_price": 10.00,
            "qty": 10,
            "stop_loss": 0.03,
            "take_profit": 0.10,
        }]
        triggered = check_stop_loss_take_profit(
            positions, stop_loss_pct=0.03, take_profit_pct=0.10,
            conviction_tp_skip=lambda sym, pct: False,
        )
        assert len(triggered) == 1
        assert triggered[0]["trigger"] == "take_profit"

    def test_skip_fn_never_blocks_stop_loss(self):
        """Stop-loss is non-negotiable. Conviction override must NEVER
        prevent SL from firing — otherwise a drawdown could spiral."""
        from portfolio_manager import check_stop_loss_take_profit

        positions = [{
            "symbol": "IONQ",
            "current_price": 9.50,      # -5% loss
            "avg_entry_price": 10.00,
            "qty": 10,
            "stop_loss": 0.03,          # 3% SL threshold
            "take_profit": 0.10,
        }]
        # Even if skip fn says True, SL must still trigger
        triggered = check_stop_loss_take_profit(
            positions, stop_loss_pct=0.03, take_profit_pct=0.10,
            conviction_tp_skip=lambda sym, pct: True,
        )
        assert len(triggered) == 1
        assert triggered[0]["trigger"] == "stop_loss"

    def test_skip_fn_never_blocks_short_take_profit(self):
        """Short TP is never overridden — shorts profit on fast reversals,
        not sustained trends."""
        from portfolio_manager import check_stop_loss_take_profit

        positions = [{
            "symbol": "IONQ",
            "current_price": 9.00,      # -10% from short entry = +10% gain for short
            "avg_entry_price": 10.00,
            "qty": -10,                  # negative qty → short
            "stop_loss": 0.08,
            "take_profit": 0.08,
        }]
        triggered = check_stop_loss_take_profit(
            positions,
            stop_loss_pct=0.03, take_profit_pct=0.10,
            short_stop_loss_pct=0.08, short_take_profit_pct=0.08,
            conviction_tp_skip=lambda sym, pct: True,
        )
        # Short TP must still fire
        assert len(triggered) == 1
        assert triggered[0]["trigger"] == "short_take_profit"

    def test_no_skip_fn_preserves_legacy_behavior(self):
        """When the override isn't enabled (no skip fn passed), behavior
        must match exactly what it did before the feature existed."""
        from portfolio_manager import check_stop_loss_take_profit

        positions = [{
            "symbol": "IONQ",
            "current_price": 12.00,
            "avg_entry_price": 10.00,
            "qty": 10,
            "stop_loss": 0.03,
            "take_profit": 0.10,
        }]
        triggered = check_stop_loss_take_profit(
            positions, stop_loss_pct=0.03, take_profit_pct=0.10,
        )
        assert len(triggered) == 1
        assert triggered[0]["trigger"] == "take_profit"


# ---------------------------------------------------------------------------
# DB-backed AI confidence lookup
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_ai_db(monkeypatch):
    tmpdir = tempfile.mkdtemp()
    monkeypatch.chdir(tmpdir)
    path = os.path.join(tmpdir, "test.db")
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE ai_predictions (
            id INTEGER PRIMARY KEY,
            timestamp TEXT,
            symbol TEXT,
            confidence REAL
        )
    """)
    conn.commit()
    conn.close()
    return path


class TestConfidenceLookup:
    def test_returns_most_recent_confidence(self, tmp_ai_db):
        from conviction_tp import _latest_ai_confidence
        conn = sqlite3.connect(tmp_ai_db)
        conn.execute("INSERT INTO ai_predictions (timestamp, symbol, confidence) VALUES (?,?,?)",
                     ("2026-04-10T10:00:00", "IONQ", 55))
        conn.execute("INSERT INTO ai_predictions (timestamp, symbol, confidence) VALUES (?,?,?)",
                     ("2026-04-15T14:00:00", "IONQ", 80))
        conn.commit()
        conn.close()
        assert _latest_ai_confidence(tmp_ai_db, "IONQ") == 80

    def test_returns_none_when_no_predictions(self, tmp_ai_db):
        from conviction_tp import _latest_ai_confidence
        assert _latest_ai_confidence(tmp_ai_db, "UNKNOWN") is None

    def test_returns_none_when_db_missing(self):
        from conviction_tp import _latest_ai_confidence
        assert _latest_ai_confidence("/nonexistent/path.db", "X") is None

    def test_returns_none_when_db_path_empty(self):
        from conviction_tp import _latest_ai_confidence
        assert _latest_ai_confidence("", "X") is None
        assert _latest_ai_confidence(None, "X") is None


# ---------------------------------------------------------------------------
# UserContext plumbing
# ---------------------------------------------------------------------------

class TestUserContextDefaults:
    def test_default_is_off(self):
        from user_context import UserContext
        ctx = UserContext(
            user_id=1, segment="small", display_name="t",
            alpaca_api_key="k", alpaca_secret_key="s",
            ai_provider="anthropic", ai_model="claude-haiku-4-5-20251001",
            ai_api_key="k", db_path=":memory:",
        )
        # Default OFF — existing profiles must not change behavior
        assert ctx.use_conviction_tp_override is False
        assert ctx.conviction_tp_min_confidence == 70.0
        assert ctx.conviction_tp_min_adx == 25.0
