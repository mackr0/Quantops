"""Tests for Layer 7 — per-symbol parameter overrides."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest


# ─────────────────────────────────────────────────────────────────────
# parse / resolve / set
# ─────────────────────────────────────────────────────────────────────

class TestParseOverrides:
    def test_round_trip(self):
        from symbol_overrides import parse_overrides
        out = parse_overrides(
            '{"max_position_pct": {"NVDA": 0.05, "KO": 0.15}}')
        assert out["max_position_pct"]["NVDA"] == 0.05
        assert out["max_position_pct"]["KO"] == 0.15

    def test_uppercase_normalized(self):
        from symbol_overrides import parse_overrides
        out = parse_overrides('{"stop_loss_pct": {"nvda": 0.05}}')
        assert "NVDA" in out["stop_loss_pct"]
        assert "nvda" not in out["stop_loss_pct"]

    def test_unknown_param_filtered(self):
        from symbol_overrides import parse_overrides
        out = parse_overrides(
            '{"not_a_real_param": {"NVDA": 0.05}, '
            '"stop_loss_pct": {"NVDA": 0.05}}')
        assert "not_a_real_param" not in out
        assert "stop_loss_pct" in out

    def test_clamps_to_bounds(self):
        from symbol_overrides import parse_overrides
        out = parse_overrides('{"stop_loss_pct": {"NVDA": 0.50}}')
        # stop_loss_pct bounds are 0.01-0.15 — 0.50 clamps to 0.15
        assert out["stop_loss_pct"]["NVDA"] == 0.15


class TestResolveParam:
    def test_no_override_returns_global(self):
        from symbol_overrides import resolve_param
        profile = {"stop_loss_pct": 0.03, "symbol_overrides": "{}"}
        assert resolve_param(profile, "stop_loss_pct", "NVDA") == 0.03

    def test_override_returned_when_symbol_matches(self):
        from symbol_overrides import resolve_param
        profile = {
            "stop_loss_pct": 0.03,
            "symbol_overrides": '{"stop_loss_pct": {"NVDA": 0.05}}',
        }
        assert resolve_param(profile, "stop_loss_pct", "NVDA") == 0.05

    def test_case_insensitive_lookup(self):
        from symbol_overrides import resolve_param
        profile = {
            "stop_loss_pct": 0.03,
            "symbol_overrides": '{"stop_loss_pct": {"NVDA": 0.05}}',
        }
        assert resolve_param(profile, "stop_loss_pct", "nvda") == 0.05

    def test_no_symbol_returns_global(self):
        from symbol_overrides import resolve_param
        profile = {
            "stop_loss_pct": 0.03,
            "symbol_overrides": '{"stop_loss_pct": {"NVDA": 0.05}}',
        }
        assert resolve_param(profile, "stop_loss_pct", None) == 0.03

    def test_unknown_symbol_returns_global(self):
        from symbol_overrides import resolve_param
        profile = {
            "stop_loss_pct": 0.03,
            "symbol_overrides": '{"stop_loss_pct": {"NVDA": 0.05}}',
        }
        assert resolve_param(profile, "stop_loss_pct", "AAPL") == 0.03


# ─────────────────────────────────────────────────────────────────────
# Tuner — _optimize_symbol_overrides
# ─────────────────────────────────────────────────────────────────────

def _make_db(tmp_path):
    db = str(tmp_path / "w8.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE ai_predictions (
            id INTEGER PRIMARY KEY,
            timestamp TEXT DEFAULT (datetime('now')),
            symbol TEXT, predicted_signal TEXT, confidence REAL,
            price_at_prediction REAL, status TEXT DEFAULT 'resolved',
            actual_outcome TEXT, actual_return_pct REAL
        );
        """
    )
    conn.commit()
    conn.close()
    return db


def _seed_symbol(db, symbol, n_total, n_wins):
    conn = sqlite3.connect(db)
    for i in range(n_wins):
        conn.execute(
            "INSERT INTO ai_predictions "
            "(symbol, predicted_signal, confidence, price_at_prediction, "
            " status, actual_outcome) "
            "VALUES (?, 'BUY', 70, 100, 'resolved', 'win')",
            (symbol,),
        )
    for i in range(n_total - n_wins):
        conn.execute(
            "INSERT INTO ai_predictions "
            "(symbol, predicted_signal, confidence, price_at_prediction, "
            " status, actual_outcome) "
            "VALUES (?, 'BUY', 70, 100, 'resolved', 'loss')",
            (symbol,),
        )
    conn.commit()
    conn.close()


class TestOptimizeSymbolOverrides:
    def test_underperforming_symbol_gets_override(self, tmp_path):
        db = _make_db(tmp_path)
        # NVDA: 25 predictions, 3 wins -> 12% WR
        # AAPL: 25 predictions, 13 wins -> 52% WR
        # Overall: 16/50 = 32% — NVDA diff = -20pt (over 15pt threshold)
        _seed_symbol(db, "NVDA", 25, 3)
        _seed_symbol(db, "AAPL", 25, 13)
        ctx = SimpleNamespace(
            profile_id=1, user_id=1, db_path=db,
            max_position_pct=0.10, ai_confidence_threshold=25,
            symbol_overrides="{}",
        )
        from self_tuning import _optimize_symbol_overrides, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                with patch("symbol_overrides.set_override") as mock_set:
                    with patch("models.log_tuning_change"):
                        msg = _optimize_symbol_overrides(
                            conn, ctx, 1, 1, overall_wr=32.0, resolved=50)
                        # NVDA underperforms — should get max_position_pct reduced
                        mock_set.assert_called()
                        args = mock_set.call_args[0]
                        assert args[1] == "max_position_pct"
                        assert args[2] == "NVDA"
                        # 0.10 * 0.75 = 0.075
                        assert args[3] == 0.075
        conn.close()
        assert msg is not None
        assert "NVDA" in msg

    def test_below_min_samples_skipped(self, tmp_path):
        db = _make_db(tmp_path)
        # NVDA only 10 samples — below the 20-sample threshold
        _seed_symbol(db, "NVDA", 10, 1)
        _seed_symbol(db, "AAPL", 25, 12)
        ctx = SimpleNamespace(
            profile_id=1, user_id=1, db_path=db,
            max_position_pct=0.10, ai_confidence_threshold=25,
            symbol_overrides="{}",
        )
        from self_tuning import _optimize_symbol_overrides, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                with patch("symbol_overrides.set_override") as mock_set:
                    msg = _optimize_symbol_overrides(
                        conn, ctx, 1, 1, overall_wr=37.0, resolved=35)
                    mock_set.assert_not_called()
        conn.close()
        assert msg is None


# ─────────────────────────────────────────────────────────────────────
# Pipeline chain — symbol > regime > TOD > global
# ─────────────────────────────────────────────────────────────────────

class TestFullChain:
    def test_per_symbol_wins_over_regime(self):
        """Per-symbol override should beat per-regime when both set."""
        from regime_overrides import resolve_for_current_regime, _regime_cache
        _regime_cache["regime"] = None
        _regime_cache["ts"] = 0
        profile = {
            "stop_loss_pct": 0.03,
            "regime_overrides": '{"stop_loss_pct": {"volatile": 0.06}}',
            "symbol_overrides": '{"stop_loss_pct": {"NVDA": 0.08}}',
            "tod_overrides": "{}",
        }
        with patch("market_regime.detect_regime",
                    return_value={"regime": "volatile"}):
            # NVDA in volatile regime: per-symbol wins (0.08 > 0.06)
            result = resolve_for_current_regime(
                profile, "stop_loss_pct", symbol="NVDA")
            assert result == 0.08

    def test_regime_wins_when_no_symbol_override(self):
        """When per-symbol override doesn't exist for the symbol, the
        regime override is used."""
        from regime_overrides import resolve_for_current_regime, _regime_cache
        _regime_cache["regime"] = None
        _regime_cache["ts"] = 0
        profile = {
            "stop_loss_pct": 0.03,
            "regime_overrides": '{"stop_loss_pct": {"volatile": 0.06}}',
            "symbol_overrides": '{"stop_loss_pct": {"NVDA": 0.08}}',
            "tod_overrides": "{}",
        }
        with patch("market_regime.detect_regime",
                    return_value={"regime": "volatile"}):
            # AAPL has no per-symbol override; falls to regime
            result = resolve_for_current_regime(
                profile, "stop_loss_pct", symbol="AAPL")
            assert result == 0.06

    def test_global_when_no_overrides(self):
        from regime_overrides import resolve_for_current_regime, _regime_cache
        _regime_cache["regime"] = None
        _regime_cache["ts"] = 0
        profile = {
            "stop_loss_pct": 0.03,
            "regime_overrides": "{}",
            "symbol_overrides": "{}",
            "tod_overrides": "{}",
        }
        with patch("market_regime.detect_regime",
                    return_value={"regime": "bull"}):
            with patch("tod_overrides._current_tod", return_value=None):
                result = resolve_for_current_regime(
                    profile, "stop_loss_pct", symbol="NVDA")
                assert result == 0.03
