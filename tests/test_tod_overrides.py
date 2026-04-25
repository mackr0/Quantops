"""Tests for Layer 4 — per-time-of-day parameter overrides."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest


# ─────────────────────────────────────────────────────────────────────
# Bucket boundaries
# ─────────────────────────────────────────────────────────────────────

class TestBucketForMinute:
    def test_open_bucket(self):
        from tod_overrides import _bucket_for_minute
        assert _bucket_for_minute(9 * 60 + 30) == "open"  # 09:30 sharp
        assert _bucket_for_minute(10 * 60) == "open"      # 10:00
        assert _bucket_for_minute(10 * 60 + 29) == "open" # 10:29

    def test_midday_bucket(self):
        from tod_overrides import _bucket_for_minute
        assert _bucket_for_minute(10 * 60 + 30) == "midday"  # 10:30
        assert _bucket_for_minute(12 * 60) == "midday"
        assert _bucket_for_minute(14 * 60 + 29) == "midday"

    def test_close_bucket(self):
        from tod_overrides import _bucket_for_minute
        assert _bucket_for_minute(14 * 60 + 30) == "close"  # 14:30
        assert _bucket_for_minute(15 * 60 + 59) == "close"

    def test_after_hours_returns_none(self):
        from tod_overrides import _bucket_for_minute
        assert _bucket_for_minute(9 * 60) is None    # 09:00 — premarket
        assert _bucket_for_minute(16 * 60) is None   # 16:00 — close
        assert _bucket_for_minute(20 * 60) is None   # 20:00 — after hours


# ─────────────────────────────────────────────────────────────────────
# parse / resolve / set
# ─────────────────────────────────────────────────────────────────────

class TestParseOverrides:
    def test_round_trip_known_buckets(self):
        from tod_overrides import parse_overrides
        out = parse_overrides(
            '{"max_position_pct": {"open": 0.05, "midday": 0.10}}')
        assert out["max_position_pct"]["open"] == 0.05
        assert out["max_position_pct"]["midday"] == 0.10

    def test_unknown_bucket_filtered(self):
        from tod_overrides import parse_overrides
        out = parse_overrides('{"max_position_pct": {"premarket": 0.05}}')
        assert "premarket" not in out.get("max_position_pct", {})

    def test_unknown_param_filtered(self):
        from tod_overrides import parse_overrides
        out = parse_overrides('{"not_a_param": {"open": 0.05}}')
        assert "not_a_param" not in out


class TestResolveParam:
    def test_no_override_returns_global(self):
        from tod_overrides import resolve_param
        profile = {"max_position_pct": 0.10, "tod_overrides": "{}"}
        assert resolve_param(profile, "max_position_pct", "open") == 0.10

    def test_override_for_matching_bucket(self):
        from tod_overrides import resolve_param
        profile = {
            "max_position_pct": 0.10,
            "tod_overrides": '{"max_position_pct": {"open": 0.05}}',
        }
        assert resolve_param(profile, "max_position_pct", "open") == 0.05
        assert resolve_param(profile, "max_position_pct", "midday") == 0.10

    def test_no_tod_returns_global(self):
        from tod_overrides import resolve_param
        profile = {
            "max_position_pct": 0.10,
            "tod_overrides": '{"max_position_pct": {"open": 0.05}}',
        }
        assert resolve_param(profile, "max_position_pct", None) == 0.10


# ─────────────────────────────────────────────────────────────────────
# Tuner — _optimize_tod_overrides
# ─────────────────────────────────────────────────────────────────────

def _make_db(tmp_path):
    db = str(tmp_path / "w6.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE ai_predictions (
            id INTEGER PRIMARY KEY,
            timestamp TEXT,
            symbol TEXT, predicted_signal TEXT, confidence REAL,
            price_at_prediction REAL, status TEXT DEFAULT 'resolved',
            actual_outcome TEXT, actual_return_pct REAL,
            features_json TEXT
        );
        """
    )
    conn.commit()
    conn.close()
    return db


def _seed_at_hour(db, et_hour, n_total, n_wins):
    """Seed predictions at a specific ET hour. Stores as UTC ISO
    (ET + 4h during EDT, +5h during EST). For test simplicity assume
    EDT — 4-hour offset."""
    conn = sqlite3.connect(db)
    utc_hour = (et_hour + 4) % 24
    ts = f"2026-04-22T{utc_hour:02d}:00:00"  # Wednesday
    for i in range(n_wins):
        conn.execute(
            "INSERT INTO ai_predictions "
            "(symbol, predicted_signal, confidence, price_at_prediction, "
            " status, actual_outcome, timestamp) "
            "VALUES (?, 'BUY', 70, 100, 'resolved', 'win', ?)",
            (f"W{et_hour}{i}", ts),
        )
    for i in range(n_total - n_wins):
        conn.execute(
            "INSERT INTO ai_predictions "
            "(symbol, predicted_signal, confidence, price_at_prediction, "
            " status, actual_outcome, timestamp) "
            "VALUES (?, 'BUY', 70, 100, 'resolved', 'loss', ?)",
            (f"L{et_hour}{i}", ts),
        )
    conn.commit()
    conn.close()


class TestOptimizeTodOverrides:
    def test_underperforming_open_bucket_gets_override(self, tmp_path):
        db = _make_db(tmp_path)
        # Open bucket (10am ET): 15 predictions, 3 wins -> 20% WR
        # Midday (12pm ET): 30 predictions, 18 wins -> 60% WR
        _seed_at_hour(db, 10, 15, 3)
        _seed_at_hour(db, 12, 30, 18)
        ctx = SimpleNamespace(
            profile_id=1, user_id=1, db_path=db,
            max_position_pct=0.10, ai_confidence_threshold=25,
            tod_overrides="{}",
        )
        from self_tuning import _optimize_tod_overrides, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                with patch("tod_overrides.set_override") as mock_set:
                    with patch("models.log_tuning_change"):
                        msg = _optimize_tod_overrides(
                            conn, ctx, 1, 1, overall_wr=46.0, resolved=45)
                        mock_set.assert_called()
                        args = mock_set.call_args[0]
                        assert args[1] == "max_position_pct"
                        assert args[2] == "open"
                        assert args[3] == 0.075
        conn.close()
        assert msg is not None
        assert "open" in msg.lower()

    def test_no_action_when_buckets_perform_uniformly(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_at_hour(db, 10, 20, 10)  # 50%
        _seed_at_hour(db, 12, 20, 10)  # 50%
        _seed_at_hour(db, 15, 20, 10)  # 50%
        ctx = SimpleNamespace(
            profile_id=1, user_id=1, db_path=db,
            max_position_pct=0.10, ai_confidence_threshold=25,
            tod_overrides="{}",
        )
        from self_tuning import _optimize_tod_overrides, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                msg = _optimize_tod_overrides(
                    conn, ctx, 1, 1, overall_wr=50.0, resolved=60)
        conn.close()
        assert msg is None


# ─────────────────────────────────────────────────────────────────────
# Pipeline chain — regime → TOD → global
# ─────────────────────────────────────────────────────────────────────

class TestPipelineChain:
    def test_regime_override_wins_over_tod_when_both_set(self):
        """Per-regime override should take precedence over per-TOD when
        both are set on the same parameter."""
        from regime_overrides import resolve_for_current_regime, _regime_cache
        _regime_cache["regime"] = None
        _regime_cache["ts"] = 0
        profile = {
            "stop_loss_pct": 0.03,
            "regime_overrides": '{"stop_loss_pct": {"volatile": 0.06}}',
            "tod_overrides": '{"stop_loss_pct": {"open": 0.05}}',
        }
        # In volatile regime, regime override should win (0.06)
        with patch("market_regime.detect_regime",
                    return_value={"regime": "volatile"}):
            result = resolve_for_current_regime(profile, "stop_loss_pct")
            assert result == 0.06

    def test_tod_override_used_when_no_regime_override(self):
        """If no per-regime override exists for the param, the per-TOD
        chain is consulted."""
        from regime_overrides import resolve_for_current_regime, _regime_cache
        _regime_cache["regime"] = None
        _regime_cache["ts"] = 0
        profile = {
            "stop_loss_pct": 0.03,
            "regime_overrides": "{}",
            "tod_overrides": '{"stop_loss_pct": {"open": 0.05}}',
        }
        with patch("market_regime.detect_regime",
                    return_value={"regime": "bull"}):
            with patch("tod_overrides._current_tod", return_value="open"):
                result = resolve_for_current_regime(profile, "stop_loss_pct")
                assert result == 0.05
