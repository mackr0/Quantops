"""Tests for the per-profile signal weights system (Layer 2)."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest


# ─────────────────────────────────────────────────────────────────────
# Storage helpers — parse / get / set / nudge
# ─────────────────────────────────────────────────────────────────────

class TestParseWeights:
    def test_empty_returns_empty_dict(self):
        from signal_weights import parse_weights
        assert parse_weights(None) == {}
        assert parse_weights("") == {}
        assert parse_weights("{}") == {}

    def test_invalid_json_returns_empty(self):
        from signal_weights import parse_weights
        assert parse_weights("not json") == {}
        assert parse_weights("[1, 2]") == {}  # Not a dict

    def test_snaps_to_ladder(self):
        from signal_weights import parse_weights, WEIGHT_LADDER
        # 0.65 should snap to 0.7 (nearest in ladder)
        out = parse_weights('{"insider_cluster": 0.65}')
        assert out["insider_cluster"] in WEIGHT_LADDER
        assert out["insider_cluster"] == 0.7

    def test_passes_ladder_values_through(self):
        from signal_weights import parse_weights
        out = parse_weights('{"a": 0.7, "b": 0.0, "c": 0.4, "d": 1.0}')
        assert out == {"a": 0.7, "b": 0.0, "c": 0.4, "d": 1.0}


class TestGetWeight:
    def test_default_is_one(self):
        from signal_weights import get_weight
        # Profile with no signal_weights at all
        profile = {"id": 1}
        assert get_weight(profile, "insider_cluster") == 1.0

    def test_returns_stored_weight(self):
        from signal_weights import get_weight
        profile = {"id": 1, "signal_weights": '{"insider_cluster": 0.4}'}
        assert get_weight(profile, "insider_cluster") == 0.4

    def test_unknown_signal_returns_one(self):
        from signal_weights import get_weight
        profile = {"id": 1, "signal_weights": '{"insider_cluster": 0.4}'}
        assert get_weight(profile, "options_signal") == 1.0


class TestNudgeUpDown:
    def test_nudge_down_moves_one_step(self):
        from signal_weights import WEIGHT_LADDER
        # The ladder is (1.0, 0.7, 0.4, 0.0), so down from 1.0 is 0.7
        # Mocked profile lookup
        with patch("models.get_trading_profile",
                    return_value={"id": 1, "signal_weights": "{}"}):
            with patch("signal_weights.set_weight") as mock_set:
                from signal_weights import nudge_down
                new = nudge_down(1, "insider_cluster")
                assert new == 0.7
                mock_set.assert_called_with(1, "insider_cluster", 0.7)

    def test_nudge_down_caps_at_zero(self):
        with patch("models.get_trading_profile",
                    return_value={"id": 1,
                                   "signal_weights": '{"insider_cluster": 0.0}'}):
            with patch("signal_weights.set_weight") as mock_set:
                from signal_weights import nudge_down
                new = nudge_down(1, "insider_cluster")
                assert new is None
                mock_set.assert_not_called()

    def test_nudge_up_caps_at_one(self):
        with patch("models.get_trading_profile",
                    return_value={"id": 1, "signal_weights": "{}"}):
            with patch("signal_weights.set_weight") as mock_set:
                from signal_weights import nudge_up
                new = nudge_up(1, "insider_cluster")
                assert new is None  # Already at 1.0 (default)
                mock_set.assert_not_called()

    def test_nudge_up_from_partial(self):
        with patch("models.get_trading_profile",
                    return_value={"id": 1,
                                   "signal_weights": '{"insider_cluster": 0.4}'}):
            with patch("signal_weights.set_weight") as mock_set:
                from signal_weights import nudge_up
                new = nudge_up(1, "insider_cluster")
                assert new == 0.7
                mock_set.assert_called_with(1, "insider_cluster", 0.7)


# ─────────────────────────────────────────────────────────────────────
# Predicates: is_signal_active
# ─────────────────────────────────────────────────────────────────────

class TestSignalPredicates:
    def test_insider_cluster_truthy(self):
        from signal_weights import is_signal_active
        assert is_signal_active("insider_cluster", {"insider_cluster": 1})
        assert not is_signal_active("insider_cluster", {"insider_cluster": 0})
        assert not is_signal_active("insider_cluster", {})

    def test_short_pct_float_threshold(self):
        from signal_weights import is_signal_active
        assert is_signal_active("short_pct_float", {"short_pct_float": 20})
        assert not is_signal_active("short_pct_float", {"short_pct_float": 10})

    def test_unknown_signal_returns_false(self):
        from signal_weights import is_signal_active
        assert not is_signal_active("not_a_real_signal", {"x": 1})


# ─────────────────────────────────────────────────────────────────────
# Tuner — _optimize_signal_weights
# ─────────────────────────────────────────────────────────────────────

def _make_db(tmp_path):
    db = str(tmp_path / "w4.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE ai_predictions (
            id INTEGER PRIMARY KEY,
            timestamp TEXT DEFAULT (datetime('now')),
            symbol TEXT, predicted_signal TEXT, confidence REAL,
            price_at_prediction REAL, status TEXT DEFAULT 'resolved',
            actual_outcome TEXT, actual_return_pct REAL,
            features_json TEXT, resolved_at TEXT
        );
        """
    )
    conn.commit()
    conn.close()
    return db


def _seed_with_signal(db, signal_present_outcomes, signal_absent_outcomes):
    """Create predictions where the insider_cluster signal is active for
    half and inactive for the other half, with specified outcomes."""
    conn = sqlite3.connect(db)
    for i, outcome in enumerate(signal_present_outcomes):
        feats = json.dumps({"insider_cluster": 1})
        conn.execute(
            "INSERT INTO ai_predictions "
            "(symbol, predicted_signal, confidence, price_at_prediction, "
            " status, actual_outcome, features_json) "
            "VALUES (?, 'BUY', 70, 100, 'resolved', ?, ?)",
            (f"P{i}", outcome, feats),
        )
    for i, outcome in enumerate(signal_absent_outcomes):
        feats = json.dumps({"some_other_signal": "neutral"})
        conn.execute(
            "INSERT INTO ai_predictions "
            "(symbol, predicted_signal, confidence, price_at_prediction, "
            " status, actual_outcome, features_json) "
            "VALUES (?, 'BUY', 70, 100, 'resolved', ?, ?)",
            (f"A{i}", outcome, feats),
        )
    conn.commit()
    conn.close()


class TestOptimizeSignalWeights:
    def test_nudges_down_when_signal_underperforms(self, tmp_path):
        db = _make_db(tmp_path)
        # 12 signal-present predictions, 3 wins -> 25% WR
        # 30 signal-absent predictions, 18 wins -> 60% WR
        # Differential: 25 - 60 = -35 pt, well past -10 threshold
        _seed_with_signal(db,
            ["win"] * 3 + ["loss"] * 9,
            ["win"] * 18 + ["loss"] * 12,
        )
        ctx = SimpleNamespace(
            profile_id=1, user_id=1, db_path=db,
            signal_weights="{}",
        )
        from self_tuning import _optimize_signal_weights, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                with patch("signal_weights.set_weight") as mock_set:
                    with patch("models.log_tuning_change"):
                        with patch("models.get_trading_profile",
                                    return_value={"id": 1, "signal_weights": "{}"}):
                            msg = _optimize_signal_weights(
                                conn, ctx, 1, 1, overall_wr=50.0, resolved=42)
                            mock_set.assert_called_with(1, "insider_cluster", 0.7)
        conn.close()
        assert msg is not None
        assert "Reduced intensity" in msg
        assert "Insider Buying Cluster" in msg

    def test_no_op_when_signal_performs_at_baseline(self, tmp_path):
        db = _make_db(tmp_path)
        # 12 signal-present, 6 wins -> 50% WR
        # 30 signal-absent, 15 wins -> 50% WR
        # Differential 0 — no change
        _seed_with_signal(db,
            ["win"] * 6 + ["loss"] * 6,
            ["win"] * 15 + ["loss"] * 15,
        )
        ctx = SimpleNamespace(
            profile_id=1, user_id=1, db_path=db,
            signal_weights="{}",
        )
        from self_tuning import _optimize_signal_weights, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                with patch("models.get_trading_profile",
                            return_value={"id": 1, "signal_weights": "{}"}):
                    msg = _optimize_signal_weights(
                        conn, ctx, 1, 1, overall_wr=50.0, resolved=42)
        conn.close()
        assert msg is None

    def test_insufficient_data_no_op(self, tmp_path):
        db = _make_db(tmp_path)
        # Only 5 predictions total — below the 30 threshold
        _seed_with_signal(db, ["loss"] * 3, ["win"] * 2)
        ctx = SimpleNamespace(
            profile_id=1, user_id=1, db_path=db,
            signal_weights="{}",
        )
        from self_tuning import _optimize_signal_weights, _get_conn
        conn = _get_conn(db)
        msg = _optimize_signal_weights(
            conn, ctx, 1, 1, overall_wr=50.0, resolved=5)
        conn.close()
        assert msg is None


# ─────────────────────────────────────────────────────────────────────
# Prompt builder integration
# ─────────────────────────────────────────────────────────────────────

class TestPromptBuilderRespectsWeights:
    def _make_market_ctx(self):
        return {"regime": "bull", "vix": 15, "spy_trend": "up"}

    def _make_candidate_with_insider_cluster(self):
        return [{
            "symbol": "X", "score": 5,
            "alt_data": {
                "insider_cluster": {
                    "is_cluster": True, "insider_count": 5,
                    "cluster_direction": "buying", "total_value": 1_000_000,
                },
            },
        }]

    def test_signal_at_full_weight_appears(self):
        from ai_analyst import _build_batch_prompt
        ctx = SimpleNamespace(signal_weights="{}",
                                max_position_pct=0.10, max_total_positions=10,
                                enable_short_selling=False, segment="small")
        prompt = _build_batch_prompt(
            self._make_candidate_with_insider_cluster(),
            {"equity": 100000, "cash": 100000, "positions": [],
             "num_positions": 0},
            self._make_market_ctx(), ctx=ctx,
        )
        assert "INSIDER CLUSTER" in prompt
        assert "intensity" not in prompt  # No hint at full weight

    def test_signal_at_partial_weight_includes_hint(self):
        from ai_analyst import _build_batch_prompt
        ctx = SimpleNamespace(
            signal_weights='{"insider_cluster": 0.4}',
            max_position_pct=0.10, max_total_positions=10,
            enable_short_selling=False, segment="small",
        )
        prompt = _build_batch_prompt(
            self._make_candidate_with_insider_cluster(),
            {"equity": 100000, "cash": 100000, "positions": [],
             "num_positions": 0},
            self._make_market_ctx(), ctx=ctx,
        )
        assert "INSIDER CLUSTER" in prompt
        assert "intensity 0.4" in prompt

    def test_signal_at_zero_weight_omitted(self):
        from ai_analyst import _build_batch_prompt
        ctx = SimpleNamespace(
            signal_weights='{"insider_cluster": 0.0}',
            max_position_pct=0.10, max_total_positions=10,
            enable_short_selling=False, segment="small",
        )
        prompt = _build_batch_prompt(
            self._make_candidate_with_insider_cluster(),
            {"equity": 100000, "cash": 100000, "positions": [],
             "num_positions": 0},
            self._make_market_ctx(), ctx=ctx,
        )
        assert "INSIDER CLUSTER" not in prompt
