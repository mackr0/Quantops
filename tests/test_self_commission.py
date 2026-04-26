"""Tests for Layer 8 — self-commissioned new strategies."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest


def _make_db(tmp_path):
    db = str(tmp_path / "w11.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE ai_predictions (
            id INTEGER PRIMARY KEY,
            timestamp TEXT DEFAULT (datetime('now')),
            symbol TEXT, predicted_signal TEXT, confidence REAL,
            price_at_prediction REAL, status TEXT DEFAULT 'resolved',
            actual_outcome TEXT, actual_return_pct REAL,
            strategy_type TEXT
        );
        """
    )
    conn.commit()
    conn.close()
    return db


def _seed_no_strategy_winners(db, n):
    conn = sqlite3.connect(db)
    for i in range(n):
        conn.execute(
            "INSERT INTO ai_predictions "
            "(symbol, predicted_signal, confidence, price_at_prediction, "
            " status, actual_outcome, actual_return_pct, strategy_type) "
            "VALUES (?, 'BUY', 70, 100, 'resolved', 'win', 3.5, NULL)",
            (f"GAP{i}",))
    conn.commit()
    conn.close()


class TestOptimizeCommissionStrategy:
    def test_no_op_when_too_few_gaps(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_no_strategy_winners(db, 3)  # below MIN_GAPS=5
        ctx = SimpleNamespace(
            profile_id=1, user_id=1, db_path=db,
            ai_provider="anthropic", ai_model="claude-haiku-4-5-20251001",
        )
        from self_tuning import _optimize_commission_strategy, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                msg = _optimize_commission_strategy(
                    conn, ctx, 1, 1, overall_wr=50.0, resolved=30)
        conn.close()
        assert msg is None

    def test_no_op_when_in_cooldown(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_no_strategy_winners(db, 10)
        ctx = SimpleNamespace(
            profile_id=1, user_id=1, db_path=db,
        )
        from self_tuning import _optimize_commission_strategy, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment",
                    return_value={"id": 1}):
            msg = _optimize_commission_strategy(
                conn, ctx, 1, 1, overall_wr=50.0, resolved=30)
        conn.close()
        assert msg is None

    def test_cost_gated_when_over_budget(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_no_strategy_winners(db, 10)
        ctx = SimpleNamespace(
            profile_id=1, user_id=1, db_path=db,
            ai_provider="anthropic", ai_model="claude-haiku-4-5-20251001",
            ai_api_key_enc="key",
        )
        from self_tuning import _optimize_commission_strategy, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                with patch("cost_guard.can_afford_action", return_value=False):
                    with patch("cost_guard.today_spend", return_value=5.0):
                        with patch("cost_guard.daily_ceiling_usd", return_value=5.0):
                            with patch("strategy_proposer.propose_strategies") as mock_prop:
                                msg = _optimize_commission_strategy(
                                    conn, ctx, 1, 1,
                                    overall_wr=50.0, resolved=30)
                                # Should NOT call the LLM
                                mock_prop.assert_not_called()
                                assert msg.startswith(
                                    "Recommendation: cost-gated")
        conn.close()

    def test_commissions_when_gaps_and_budget_ok(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_no_strategy_winners(db, 10)
        ctx = SimpleNamespace(
            profile_id=1, user_id=1, db_path=db,
            ai_provider="anthropic", ai_model="claude-haiku-4-5-20251001",
            ai_api_key="key",
        )
        from self_tuning import _optimize_commission_strategy, _get_conn
        conn = _get_conn(db)
        fake_spec = {
            "name": "test", "description": "test strategy",
            "applicable_markets": ["small"],
            "trigger_conditions": [],
            "side": "BUY",
        }
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                with patch("cost_guard.can_afford_action", return_value=True):
                    with patch("strategy_proposer.propose_strategies",
                                return_value=[fake_spec]):
                        with patch("strategy_generator.save_spec",
                                    return_value=99):
                            with patch("models.log_tuning_change"):
                                msg = _optimize_commission_strategy(
                                    conn, ctx, 1, 1,
                                    overall_wr=50.0, resolved=30)
        conn.close()
        assert msg is not None
        assert "Commissioned" in msg

    def test_returns_none_when_proposer_returns_empty(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_no_strategy_winners(db, 10)
        ctx = SimpleNamespace(
            profile_id=1, user_id=1, db_path=db,
            ai_provider="anthropic", ai_model="claude-haiku-4-5-20251001",
            ai_api_key="key",
        )
        from self_tuning import _optimize_commission_strategy, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                with patch("cost_guard.can_afford_action", return_value=True):
                    with patch("strategy_proposer.propose_strategies",
                                return_value=[]):
                        msg = _optimize_commission_strategy(
                            conn, ctx, 1, 1,
                            overall_wr=50.0, resolved=30)
        conn.close()
        assert msg is None
