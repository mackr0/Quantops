"""Wave 2 — entry filter optimizers (Layer 1 Group C). Each rule reads
from features_json on resolved predictions, buckets entries by which
side of the threshold they fell on, and adjusts when the marginal
bucket underperforms.

These tests use synthetic features_json data to exercise each rule's
detection logic and verify cooldown / bound respect.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _make_db(tmp_path):
    db = str(tmp_path / "w2.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE ai_predictions (
            id INTEGER PRIMARY KEY,
            timestamp TEXT DEFAULT (datetime('now')),
            symbol TEXT, predicted_signal TEXT, confidence REAL,
            price_at_prediction REAL, status TEXT DEFAULT 'resolved',
            actual_outcome TEXT, actual_return_pct REAL,
            regime_at_prediction TEXT, strategy_type TEXT,
            features_json TEXT, resolved_at TEXT, resolution_price REAL,
            days_held INTEGER
        );
        """
    )
    conn.commit()
    conn.close()
    return db


def _ctx(db, **overrides):
    defaults = dict(
        profile_id=1, user_id=1, db_path=db, enable_self_tuning=True,
        display_name="Test", segment="small",
        min_volume=500_000,
        volume_surge_multiplier=2.0,
        breakout_volume_threshold=1.0,
        gap_pct_threshold=3.0,
        momentum_5d_gain=3.0,
        momentum_20d_gain=5.0,
        rsi_overbought=85.0,
        rsi_oversold=25.0,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _seed_with_feature(db, feature_name, rows):
    """rows = list of (feature_value, outcome) tuples."""
    conn = sqlite3.connect(db)
    for i, (val, outcome) in enumerate(rows):
        feats = {feature_name: val}
        conn.execute(
            "INSERT INTO ai_predictions "
            "(symbol, predicted_signal, confidence, price_at_prediction, "
            " status, actual_outcome, features_json) "
            "VALUES (?, 'BUY', 70, 100, 'resolved', ?, ?)",
            (f"S{i}", outcome, json.dumps(feats)),
        )
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────────────────────────────
# min_volume
# ─────────────────────────────────────────────────────────────────────

class TestMinVolume:
    def test_raises_when_marginal_volume_entries_lose(self, tmp_path):
        db = _make_db(tmp_path)
        # 10 entries at volume 600K-750K (within 1.5x of 500K min), all losers
        _seed_with_feature(db, "volume",
            [(650_000, "loss")] * 10)
        ctx = _ctx(db, min_volume=500_000)
        from self_tuning import _optimize_min_volume, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                with patch("models.update_trading_profile") as mock_up:
                    with patch("models.log_tuning_change"):
                        msg = _optimize_min_volume(
                            conn, ctx, 1, 1, overall_wr=45.0, resolved=20)
                        mock_up.assert_called_with(1, min_volume=750_000)
        conn.close()
        assert msg is not None
        assert "750,000" in msg

    def test_no_op_without_features(self, tmp_path):
        db = _make_db(tmp_path)
        ctx = _ctx(db)
        from self_tuning import _optimize_min_volume, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            msg = _optimize_min_volume(
                conn, ctx, 1, 1, overall_wr=45.0, resolved=0)
        conn.close()
        assert msg is None


# ─────────────────────────────────────────────────────────────────────
# volume_surge_multiplier
# ─────────────────────────────────────────────────────────────────────

class TestVolumeSurgeMultiplier:
    def test_tightens_when_marginal_surge_entries_lose(self, tmp_path):
        db = _make_db(tmp_path)
        # volume_ratio between 2.0 and 2.5 (within 1.25x of 2.0), all losers
        _seed_with_feature(db, "volume_ratio",
            [(2.2, "loss")] * 8)
        ctx = _ctx(db, volume_surge_multiplier=2.0)
        from self_tuning import _optimize_volume_surge_multiplier, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                with patch("models.update_trading_profile") as mock_up:
                    with patch("models.log_tuning_change"):
                        msg = _optimize_volume_surge_multiplier(
                            conn, ctx, 1, 1, overall_wr=45.0, resolved=20)
                        mock_up.assert_called_with(1, volume_surge_multiplier=2.25)
        conn.close()
        assert msg is not None


# ─────────────────────────────────────────────────────────────────────
# gap_pct_threshold
# ─────────────────────────────────────────────────────────────────────

class TestGapPctThreshold:
    def test_raises_when_marginal_gap_entries_lose(self, tmp_path):
        db = _make_db(tmp_path)
        # gap_pct 3.0-3.6 (within 1.2x), all losers
        _seed_with_feature(db, "gap_pct",
            [(3.3, "loss")] * 8)
        ctx = _ctx(db, gap_pct_threshold=3.0)
        from self_tuning import _optimize_gap_pct_threshold, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                with patch("models.update_trading_profile") as mock_up:
                    with patch("models.log_tuning_change"):
                        msg = _optimize_gap_pct_threshold(
                            conn, ctx, 1, 1, overall_wr=45.0, resolved=20)
                        mock_up.assert_called_with(1, gap_pct_threshold=3.5)
        conn.close()
        assert msg is not None


# ─────────────────────────────────────────────────────────────────────
# momentum_5d / momentum_20d
# ─────────────────────────────────────────────────────────────────────

class TestMomentumThresholds:
    def test_5d_raises_when_marginal_lose(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_with_feature(db, "momentum_5d",
            [(3.3, "loss")] * 8)
        ctx = _ctx(db, momentum_5d_gain=3.0)
        from self_tuning import _optimize_momentum_5d, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                with patch("models.update_trading_profile") as mock_up:
                    with patch("models.log_tuning_change"):
                        msg = _optimize_momentum_5d(
                            conn, ctx, 1, 1, overall_wr=45.0, resolved=20)
                        mock_up.assert_called_with(1, momentum_5d_gain=3.5)
        conn.close()
        assert msg is not None

    def test_20d_raises_when_marginal_lose(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_with_feature(db, "momentum_20d",
            [(5.5, "loss")] * 8)
        ctx = _ctx(db, momentum_20d_gain=5.0)
        from self_tuning import _optimize_momentum_20d, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                with patch("models.update_trading_profile") as mock_up:
                    with patch("models.log_tuning_change"):
                        msg = _optimize_momentum_20d(
                            conn, ctx, 1, 1, overall_wr=45.0, resolved=20)
                        mock_up.assert_called_with(1, momentum_20d_gain=5.5)
        conn.close()
        assert msg is not None


# ─────────────────────────────────────────────────────────────────────
# RSI bands
# ─────────────────────────────────────────────────────────────────────

class TestRsiBands:
    def test_overbought_raised_when_high_rsi_entries_won(self, tmp_path):
        db = _make_db(tmp_path)
        # RSI 80-90 (within ±5 of 85), 7 wins / 3 losses = 70% WR
        _seed_with_feature(db, "rsi",
            [(82, "win")] * 7 + [(87, "loss")] * 3)
        ctx = _ctx(db, rsi_overbought=85.0)
        from self_tuning import _optimize_rsi_overbought, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                with patch("models.update_trading_profile") as mock_up:
                    with patch("models.log_tuning_change"):
                        msg = _optimize_rsi_overbought(
                            conn, ctx, 1, 1, overall_wr=50.0, resolved=20)
                        mock_up.assert_called_with(1, rsi_overbought=87.0)
        conn.close()
        assert msg is not None

    def test_oversold_lowered_when_low_rsi_entries_won(self, tmp_path):
        db = _make_db(tmp_path)
        # RSI 20-30, 7 wins / 3 losses
        _seed_with_feature(db, "rsi",
            [(22, "win")] * 7 + [(27, "loss")] * 3)
        ctx = _ctx(db, rsi_oversold=25.0)
        from self_tuning import _optimize_rsi_oversold, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                with patch("models.update_trading_profile") as mock_up:
                    with patch("models.log_tuning_change"):
                        msg = _optimize_rsi_oversold(
                            conn, ctx, 1, 1, overall_wr=50.0, resolved=20)
                        mock_up.assert_called_with(1, rsi_oversold=23.0)
        conn.close()
        assert msg is not None

    def test_overbought_no_op_when_band_is_balanced(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_with_feature(db, "rsi",
            [(85, "win")] * 5 + [(85, "loss")] * 5)
        ctx = _ctx(db, rsi_overbought=85.0)
        from self_tuning import _optimize_rsi_overbought, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                msg = _optimize_rsi_overbought(
                    conn, ctx, 1, 1, overall_wr=50.0, resolved=20)
        conn.close()
        assert msg is None


# ─────────────────────────────────────────────────────────────────────
# Cooldown applies uniformly
# ─────────────────────────────────────────────────────────────────────

class TestCooldownAcrossW2Rules:
    def test_min_volume_respects_cooldown(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_with_feature(db, "volume", [(650_000, "loss")] * 10)
        ctx = _ctx(db, min_volume=500_000)
        from self_tuning import _optimize_min_volume, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment",
                   return_value={"id": 1}):
            msg = _optimize_min_volume(
                conn, ctx, 1, 1, overall_wr=45.0, resolved=20)
        conn.close()
        assert msg is None


# ─────────────────────────────────────────────────────────────────────
# Orchestrator registration
# ─────────────────────────────────────────────────────────────────────

class TestW2OptimizerRegistration:
    def test_all_w2_optimizers_registered(self):
        import self_tuning
        import inspect
        src = inspect.getsource(self_tuning._apply_upward_optimizations)
        for fname in [
            "_optimize_min_volume",
            "_optimize_volume_surge_multiplier",
            "_optimize_breakout_volume_threshold",
            "_optimize_gap_pct_threshold",
            "_optimize_momentum_5d",
            "_optimize_momentum_20d",
            "_optimize_rsi_overbought",
            "_optimize_rsi_oversold",
        ]:
            assert fname in src, f"{fname} not registered in orchestrator"
