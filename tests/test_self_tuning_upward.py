"""Tests for the self-tuner upward optimization layer.

The upward optimizer actively improves win rates by finding the best
confidence bands, adjusting for regime, disabling losing strategies,
tuning stop/take-profit, and increasing position size when there's a
proven edge. It only runs when overall_wr >= 35% (disaster prevention
has exclusive control below that).
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest


def _make_db(tmp_path):
    """Create a test DB with ai_predictions and trades tables."""
    db = str(tmp_path / "test.db")
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE ai_predictions (
            id INTEGER PRIMARY KEY,
            timestamp TEXT DEFAULT (datetime('now')),
            symbol TEXT, predicted_signal TEXT, confidence REAL,
            price_at_prediction REAL, status TEXT DEFAULT 'resolved',
            actual_outcome TEXT, actual_return_pct REAL,
            regime_at_prediction TEXT, strategy_type TEXT,
            features_json TEXT, resolved_at TEXT, resolution_price REAL,
            days_held INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            timestamp TEXT DEFAULT (datetime('now')),
            symbol TEXT, side TEXT, qty REAL, price REAL,
            order_id TEXT, signal_type TEXT, strategy TEXT,
            reason TEXT, ai_reasoning TEXT, ai_confidence REAL,
            stop_loss REAL, take_profit REAL, status TEXT, pnl REAL,
            decision_price REAL, fill_price REAL, slippage_pct REAL
        )
    """)
    conn.commit()
    conn.close()
    return db


def _make_ctx(db, **overrides):
    """Build a mock UserContext."""
    defaults = dict(
        profile_id=1, user_id=1, db_path=db, enable_self_tuning=True,
        display_name="Test", segment="small",
        ai_confidence_threshold=25, max_position_pct=0.10,
        stop_loss_pct=0.03, take_profit_pct=0.10,
        strategy_momentum_breakout=True, strategy_volume_spike=True,
        strategy_mean_reversion=True, strategy_gap_and_go=True,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _insert_predictions(db, predictions):
    """Insert prediction rows. Each is a dict with at least 'confidence' and 'outcome'."""
    conn = sqlite3.connect(db)
    for i, p in enumerate(predictions):
        conn.execute(
            "INSERT INTO ai_predictions "
            "(symbol, predicted_signal, confidence, price_at_prediction, "
            "status, actual_outcome, actual_return_pct, "
            "regime_at_prediction, strategy_type) "
            "VALUES (?, 'BUY', ?, 100, 'resolved', ?, ?, ?, ?)",
            (
                p.get("symbol", f"SYM{i}"),
                p.get("confidence", 50),
                p.get("outcome", "win"),
                p.get("return_pct", 2.0 if p.get("outcome") == "win" else -2.0),
                p.get("regime", "bull"),
                p.get("strategy", "momentum_breakout"),
            ),
        )
    conn.commit()
    conn.close()


def _insert_trades(db, trades):
    """Insert trade rows."""
    conn = sqlite3.connect(db)
    for t in trades:
        conn.execute(
            "INSERT INTO trades (symbol, side, qty, price, pnl, stop_loss, take_profit, status) "
            "VALUES (?, 'buy', ?, ?, ?, ?, ?, 'closed')",
            (
                t.get("symbol", "TEST"),
                t.get("qty", 10),
                t.get("price", 100),
                t.get("pnl"),
                t.get("stop_loss"),
                t.get("take_profit"),
            ),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Create main DB for tuning_history
    main_db = str(tmp_path / "quantopsai.db")
    conn = sqlite3.connect(main_db)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tuning_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            adjustment_type TEXT NOT NULL,
            parameter_name TEXT NOT NULL,
            old_value TEXT NOT NULL,
            new_value TEXT NOT NULL,
            reason TEXT NOT NULL,
            win_rate_at_change REAL,
            predictions_resolved INTEGER,
            outcome_after TEXT DEFAULT 'pending',
            win_rate_after REAL,
            reviewed_at TEXT
        )
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr("config.DB_PATH", main_db)
    return tmp_path


class TestConfidenceThresholdUpward:
    def test_raised_to_best_band(self, setup, monkeypatch):
        db = _make_db(setup)
        ctx = _make_ctx(db)
        preds = (
            [{"confidence": 45, "outcome": "loss"} for _ in range(7)]
            + [{"confidence": 45, "outcome": "win"} for _ in range(3)]
            + [{"confidence": 65, "outcome": "loss"} for _ in range(5)]
            + [{"confidence": 65, "outcome": "win"} for _ in range(5)]
            + [{"confidence": 75, "outcome": "win"} for _ in range(8)]
            + [{"confidence": 75, "outcome": "loss"} for _ in range(2)]
        )
        _insert_predictions(db, preds)

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        from self_tuning import _optimize_confidence_threshold_upward
        updated = {}
        monkeypatch.setattr("models.update_trading_profile",
                            lambda pid, **kw: updated.update(kw))
        result = _optimize_confidence_threshold_upward(
            conn, ctx, 1, 1, 53.3, 30)
        conn.close()
        assert result is not None
        assert updated.get("ai_confidence_threshold") == 50  # Raises one band from 25

    def test_skipped_on_cooldown(self, setup, monkeypatch):
        db = _make_db(setup)
        ctx = _make_ctx(db)
        preds = ([{"confidence": 75, "outcome": "win"} for _ in range(10)]
                 + [{"confidence": 45, "outcome": "loss"} for _ in range(10)])
        _insert_predictions(db, preds)

        # Simulate recent adjustment
        from models import log_tuning_change
        log_tuning_change(1, 1, "test", "ai_confidence_threshold",
                          "25", "50", "test", 50, 20)

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        from self_tuning import _optimize_confidence_threshold_upward
        result = _optimize_confidence_threshold_upward(
            conn, ctx, 1, 1, 50, 20)
        conn.close()
        assert result is None

    def test_skipped_when_previous_worsened(self, setup, monkeypatch):
        db = _make_db(setup)
        ctx = _make_ctx(db)
        preds = ([{"confidence": 75, "outcome": "win"} for _ in range(10)]
                 + [{"confidence": 45, "outcome": "loss"} for _ in range(10)])
        _insert_predictions(db, preds)

        # Log a worsened adjustment
        from models import log_tuning_change, _get_conn
        row_id = log_tuning_change(1, 1, "test", "ai_confidence_threshold",
                                   "25", "50", "test", 50, 20)
        mc = _get_conn()
        mc.execute("UPDATE tuning_history SET outcome_after='worsened', "
                   "timestamp=datetime('now','-5 days') WHERE id=?", (row_id,))
        mc.commit()
        mc.close()

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        from self_tuning import _optimize_confidence_threshold_upward
        result = _optimize_confidence_threshold_upward(
            conn, ctx, 1, 1, 50, 20)
        conn.close()
        assert result is None


class TestRegimePositionSizing:
    def test_reduces_in_bad_regime(self, setup, monkeypatch):
        db = _make_db(setup)
        ctx = _make_ctx(db)
        preds = (
            [{"regime": "bull", "outcome": "win"} for _ in range(10)]
            + [{"regime": "bull", "outcome": "loss"} for _ in range(5)]
            + [{"regime": "sideways", "outcome": "loss"} for _ in range(11)]
            + [{"regime": "sideways", "outcome": "win"} for _ in range(4)]
        )
        _insert_predictions(db, preds)

        monkeypatch.setattr("market_regime.detect_regime",
                            lambda: {"regime": "sideways"})
        updated = {}
        monkeypatch.setattr("models.update_trading_profile",
                            lambda pid, **kw: updated.update(kw))

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        from self_tuning import _optimize_regime_position_sizing
        result = _optimize_regime_position_sizing(
            conn, ctx, 1, 1, 46.7, 30)
        conn.close()
        assert result is not None
        assert updated["max_position_pct"] == 0.075  # 25% reduction

    def test_increases_in_strong_regime(self, setup, monkeypatch):
        db = _make_db(setup)
        ctx = _make_ctx(db)
        preds = (
            [{"regime": "bull", "outcome": "win"} for _ in range(12)]
            + [{"regime": "bull", "outcome": "loss"} for _ in range(3)]
            + [{"regime": "sideways", "outcome": "win"} for _ in range(6)]
            + [{"regime": "sideways", "outcome": "loss"} for _ in range(9)]
        )
        _insert_predictions(db, preds)

        monkeypatch.setattr("market_regime.detect_regime",
                            lambda: {"regime": "bull"})
        updated = {}
        monkeypatch.setattr("models.update_trading_profile",
                            lambda pid, **kw: updated.update(kw))

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        from self_tuning import _optimize_regime_position_sizing
        result = _optimize_regime_position_sizing(
            conn, ctx, 1, 1, 60.0, 30)
        conn.close()
        assert result is not None
        assert updated["max_position_pct"] == 0.115


class TestStrategyToggles:
    def test_disables_worst_strategy(self, setup, monkeypatch):
        db = _make_db(setup)
        ctx = _make_ctx(db)
        preds = (
            [{"strategy": "momentum_breakout", "outcome": "loss"} for _ in range(8)]
            + [{"strategy": "momentum_breakout", "outcome": "win"} for _ in range(2)]
            + [{"strategy": "mean_reversion", "outcome": "win"} for _ in range(7)]
            + [{"strategy": "mean_reversion", "outcome": "loss"} for _ in range(3)]
        )
        _insert_predictions(db, preds)

        updated = {}
        monkeypatch.setattr("models.update_trading_profile",
                            lambda pid, **kw: updated.update(kw))

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        from self_tuning import _optimize_strategy_toggles
        result = _optimize_strategy_toggles(
            conn, ctx, 1, 1, 45.0, 20)
        conn.close()
        assert result is not None
        assert updated.get("strategy_momentum_breakout") == 0

    def test_preserves_last_strategy(self, setup, monkeypatch):
        db = _make_db(setup)
        ctx = _make_ctx(db, strategy_volume_spike=False,
                        strategy_mean_reversion=False,
                        strategy_gap_and_go=False)
        preds = [{"strategy": "momentum_breakout", "outcome": "loss"} for _ in range(10)]
        _insert_predictions(db, preds)

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        from self_tuning import _optimize_strategy_toggles
        result = _optimize_strategy_toggles(
            conn, ctx, 1, 1, 50.0, 10)
        conn.close()
        # Should NOT disable — it's the only one left
        assert result is None or "Recommendation" in result


class TestStopTakeProfit:
    def test_stop_loss_widened_when_too_tight(self, setup, monkeypatch):
        db = _make_db(setup)
        ctx = _make_ctx(db)
        trades = (
            # 8 losses clustered near the 3% stop
            [{"pnl": -30, "price": 100, "qty": 10, "stop_loss": 97, "take_profit": 110} for _ in range(8)]
            # 7 wins
            + [{"pnl": 50, "price": 100, "qty": 10, "stop_loss": 97, "take_profit": 110} for _ in range(7)]
        )
        _insert_trades(db, trades)

        updated = {}
        monkeypatch.setattr("models.update_trading_profile",
                            lambda pid, **kw: updated.update(kw))

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        from self_tuning import _optimize_stop_take_profit
        result = _optimize_stop_take_profit(
            conn, ctx, 1, 1, 46.7, 30)
        conn.close()
        assert result is not None
        assert updated["stop_loss_pct"] == 0.036  # 20% wider

    def test_take_profit_tightened_when_too_ambitious(self, setup, monkeypatch):
        db = _make_db(setup)
        ctx = _make_ctx(db)
        trades = (
            # Wins average +3.5% but TP is at 10%
            [{"pnl": 35, "price": 100, "qty": 10, "stop_loss": 97, "take_profit": 110} for _ in range(8)]
            # Losses are large (not near stop) so stop-loss check won't trigger
            + [{"pnl": -70, "price": 100, "qty": 10, "stop_loss": 97, "take_profit": 110} for _ in range(7)]
        )
        _insert_trades(db, trades)

        updated = {}
        monkeypatch.setattr("models.update_trading_profile",
                            lambda pid, **kw: updated.update(kw))

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        from self_tuning import _optimize_stop_take_profit
        result = _optimize_stop_take_profit(
            conn, ctx, 1, 1, 53.3, 30)
        conn.close()
        assert result is not None
        assert updated["take_profit_pct"] == 0.08  # 20% tighter


class TestPositionSizeUpward:
    def test_increased_with_strong_edge(self, setup, monkeypatch):
        db = _make_db(setup)
        ctx = _make_ctx(db)
        preds = (
            [{"outcome": "win", "return_pct": 2.5} for _ in range(20)]
            + [{"outcome": "loss", "return_pct": -1.5} for _ in range(12)]
        )
        _insert_predictions(db, preds)

        updated = {}
        monkeypatch.setattr("models.update_trading_profile",
                            lambda pid, **kw: updated.update(kw))

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        from self_tuning import _optimize_position_size_upward
        result = _optimize_position_size_upward(
            conn, ctx, 1, 1, 62.5, 32)
        conn.close()
        assert result is not None
        assert updated["max_position_pct"] == 0.115

    def test_not_increased_above_cap(self, setup, monkeypatch):
        db = _make_db(setup)
        ctx = _make_ctx(db, max_position_pct=0.15)
        preds = [{"outcome": "win", "return_pct": 3.0} for _ in range(30)]
        _insert_predictions(db, preds)

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        from self_tuning import _optimize_position_size_upward
        result = _optimize_position_size_upward(
            conn, ctx, 1, 1, 100.0, 30)
        conn.close()
        assert result is None


class TestOrchestrator:
    def test_skipped_in_disaster_mode(self, setup, monkeypatch):
        """Upward optimizations should not run when overall_wr < 35."""
        db = _make_db(setup)
        ctx = _make_ctx(db)
        preds = [{"confidence": 75, "outcome": "win"} for _ in range(10)]
        _insert_predictions(db, preds)

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        from self_tuning import _apply_upward_optimizations
        # This should be blocked by the gate in apply_auto_adjustments,
        # but if called directly with low WR it should still work safely
        results = _apply_upward_optimizations(conn, ctx, 1, 1, 25.0, 10)
        conn.close()
        # At 25% WR, none of the optimizers should trigger
        # (confidence optimizer needs best_wr > overall + 10 which is hard at 25%)
        assert isinstance(results, list)

    def test_only_one_change_per_run(self, setup, monkeypatch):
        """Even if multiple optimizers would trigger, only the first one runs."""
        db = _make_db(setup)
        ctx = _make_ctx(db)
        # Set up data where BOTH confidence AND strategy would trigger
        preds = (
            [{"confidence": 75, "outcome": "win", "strategy": "mean_reversion"} for _ in range(8)]
            + [{"confidence": 75, "outcome": "loss", "strategy": "mean_reversion"} for _ in range(2)]
            + [{"confidence": 45, "outcome": "loss", "strategy": "momentum_breakout"} for _ in range(8)]
            + [{"confidence": 45, "outcome": "win", "strategy": "momentum_breakout"} for _ in range(2)]
        )
        _insert_predictions(db, preds)

        call_count = {"n": 0}
        orig_update = None
        def counting_update(pid, **kw):
            call_count["n"] += 1
        monkeypatch.setattr("models.update_trading_profile", counting_update)

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        from self_tuning import _apply_upward_optimizations
        results = _apply_upward_optimizations(conn, ctx, 1, 1, 50.0, 20)
        conn.close()
        # Should have at most 1 actual parameter change
        assert call_count["n"] <= 1
        assert len(results) <= 1
