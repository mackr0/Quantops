"""Tests for Layer 3 — per-regime parameter overrides."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest


# ─────────────────────────────────────────────────────────────────────
# Storage / parsing
# ─────────────────────────────────────────────────────────────────────

class TestParseOverrides:
    def test_empty_returns_empty_dict(self):
        from regime_overrides import parse_overrides
        assert parse_overrides(None) == {}
        assert parse_overrides("") == {}
        assert parse_overrides("{}") == {}

    def test_invalid_json_returns_empty(self):
        from regime_overrides import parse_overrides
        assert parse_overrides("not json") == {}
        assert parse_overrides("[1, 2]") == {}

    def test_valid_overrides_pass_through(self):
        from regime_overrides import parse_overrides
        out = parse_overrides(
            '{"stop_loss_pct": {"volatile": 0.05, "crisis": 0.08}}')
        assert out == {"stop_loss_pct": {"volatile": 0.05, "crisis": 0.08}}

    def test_unknown_regime_filtered(self):
        from regime_overrides import parse_overrides
        out = parse_overrides(
            '{"stop_loss_pct": {"volatile": 0.05, "yolo": 0.99}}')
        assert "yolo" not in out["stop_loss_pct"]
        assert out["stop_loss_pct"]["volatile"] == 0.05

    def test_unknown_param_filtered(self):
        from regime_overrides import parse_overrides
        out = parse_overrides(
            '{"not_a_param": {"volatile": 0.05}, "stop_loss_pct": {"bull": 0.03}}')
        assert "not_a_param" not in out
        assert "stop_loss_pct" in out

    def test_clamps_out_of_range_values(self):
        from regime_overrides import parse_overrides
        # max_correlation bounds: 0.30-0.95
        out = parse_overrides(
            '{"max_correlation": {"crisis": 1.5}}')
        assert out["max_correlation"]["crisis"] == 0.95


# ─────────────────────────────────────────────────────────────────────
# resolve_param fallback chain
# ─────────────────────────────────────────────────────────────────────

class TestResolveParam:
    def test_no_override_returns_global(self):
        from regime_overrides import resolve_param
        profile = {"stop_loss_pct": 0.03, "regime_overrides": "{}"}
        assert resolve_param(profile, "stop_loss_pct", "volatile") == 0.03

    def test_override_returned_when_regime_matches(self):
        from regime_overrides import resolve_param
        profile = {
            "stop_loss_pct": 0.03,
            "regime_overrides": '{"stop_loss_pct": {"volatile": 0.06}}',
        }
        assert resolve_param(profile, "stop_loss_pct", "volatile") == 0.06

    def test_override_only_for_specified_regime(self):
        from regime_overrides import resolve_param
        profile = {
            "stop_loss_pct": 0.03,
            "regime_overrides": '{"stop_loss_pct": {"volatile": 0.06}}',
        }
        # Bull regime -> falls back to global
        assert resolve_param(profile, "stop_loss_pct", "bull") == 0.03

    def test_no_regime_returns_global(self):
        from regime_overrides import resolve_param
        profile = {
            "stop_loss_pct": 0.03,
            "regime_overrides": '{"stop_loss_pct": {"volatile": 0.06}}',
        }
        assert resolve_param(profile, "stop_loss_pct", None) == 0.03

    def test_unknown_regime_returns_global(self):
        from regime_overrides import resolve_param
        profile = {
            "stop_loss_pct": 0.03,
            "regime_overrides": '{"stop_loss_pct": {"volatile": 0.06}}',
        }
        assert resolve_param(profile, "stop_loss_pct", "unknown") == 0.03

    def test_works_with_namespace_object(self):
        from regime_overrides import resolve_param
        ctx = SimpleNamespace(
            stop_loss_pct=0.03,
            regime_overrides='{"stop_loss_pct": {"volatile": 0.06}}',
        )
        assert resolve_param(ctx, "stop_loss_pct", "volatile") == 0.06


# ─────────────────────────────────────────────────────────────────────
# resolve_for_current_regime — auto-detects regime
# ─────────────────────────────────────────────────────────────────────

class TestResolveForCurrentRegime:
    def test_uses_detected_regime(self):
        from regime_overrides import resolve_for_current_regime, _regime_cache
        # Reset cache
        _regime_cache["regime"] = None
        _regime_cache["ts"] = 0
        profile = {
            "stop_loss_pct": 0.03,
            "regime_overrides": '{"stop_loss_pct": {"crisis": 0.10}}',
        }
        with patch("market_regime.detect_regime",
                    return_value={"regime": "crisis"}):
            assert resolve_for_current_regime(
                profile, "stop_loss_pct") == 0.10

    def test_falls_back_when_detection_fails(self):
        from regime_overrides import resolve_for_current_regime, _regime_cache
        _regime_cache["regime"] = None
        _regime_cache["ts"] = 0
        profile = {"stop_loss_pct": 0.03, "regime_overrides": "{}"}
        with patch("market_regime.detect_regime",
                    side_effect=Exception("no data")):
            assert resolve_for_current_regime(
                profile, "stop_loss_pct", default=0.03) == 0.03


# ─────────────────────────────────────────────────────────────────────
# Tuner — _optimize_regime_overrides
# ─────────────────────────────────────────────────────────────────────

def _make_db(tmp_path):
    db = str(tmp_path / "w5.db")
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


def _seed_regime_predictions(db, regime, n_total, n_wins):
    conn = sqlite3.connect(db)
    for i in range(n_wins):
        conn.execute(
            "INSERT INTO ai_predictions "
            "(symbol, predicted_signal, confidence, price_at_prediction, "
            " status, actual_outcome, regime_at_prediction) "
            "VALUES (?, 'BUY', 70, 100, 'resolved', 'win', ?)",
            (f"W{regime}{i}", regime),
        )
    for i in range(n_total - n_wins):
        conn.execute(
            "INSERT INTO ai_predictions "
            "(symbol, predicted_signal, confidence, price_at_prediction, "
            " status, actual_outcome, regime_at_prediction) "
            "VALUES (?, 'BUY', 70, 100, 'resolved', 'loss', ?)",
            (f"L{regime}{i}", regime),
        )
    conn.commit()
    conn.close()


class TestOptimizeRegimeOverrides:
    def test_creates_override_for_underperforming_regime(self, tmp_path):
        db = _make_db(tmp_path)
        # bull: 30 predictions, 18 wins -> 60% WR
        # volatile: 15 predictions, 4 wins -> 27% WR (underperforms baseline by 33pt)
        _seed_regime_predictions(db, "bull", 30, 18)
        _seed_regime_predictions(db, "volatile", 15, 4)
        ctx = SimpleNamespace(
            profile_id=1, user_id=1, db_path=db,
            max_position_pct=0.10, ai_confidence_threshold=25,
            regime_overrides="{}",
        )
        from self_tuning import _optimize_regime_overrides, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                with patch("regime_overrides.set_override") as mock_set:
                    with patch("models.log_tuning_change"):
                        msg = _optimize_regime_overrides(
                            conn, ctx, 1, 1, overall_wr=49.0, resolved=45)
                        # Underperforming regime (volatile) should get
                        # max_position_pct reduced
                        mock_set.assert_called()
                        args = mock_set.call_args[0]
                        assert args[1] == "max_position_pct"
                        assert args[2] == "volatile"
                        # 0.10 * 0.75 = 0.075
                        assert args[3] == 0.075
        conn.close()
        assert msg is not None
        assert "volatile" in msg

    def test_no_action_when_regimes_perform_uniformly(self, tmp_path):
        db = _make_db(tmp_path)
        # Both regimes ~50% WR — no differential
        _seed_regime_predictions(db, "bull", 20, 10)
        _seed_regime_predictions(db, "volatile", 20, 10)
        ctx = SimpleNamespace(
            profile_id=1, user_id=1, db_path=db,
            max_position_pct=0.10, ai_confidence_threshold=25,
            regime_overrides="{}",
        )
        from self_tuning import _optimize_regime_overrides, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                msg = _optimize_regime_overrides(
                    conn, ctx, 1, 1, overall_wr=50.0, resolved=40)
        conn.close()
        assert msg is None

    def test_no_action_when_sample_too_small(self, tmp_path):
        db = _make_db(tmp_path)
        # Only 5 in volatile — below the min sample threshold
        _seed_regime_predictions(db, "bull", 30, 18)
        _seed_regime_predictions(db, "volatile", 5, 0)
        ctx = SimpleNamespace(
            profile_id=1, user_id=1, db_path=db,
            max_position_pct=0.10, ai_confidence_threshold=25,
            regime_overrides="{}",
        )
        from self_tuning import _optimize_regime_overrides, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                msg = _optimize_regime_overrides(
                    conn, ctx, 1, 1, overall_wr=49.0, resolved=35)
        conn.close()
        assert msg is None
