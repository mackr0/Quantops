"""Tests for Layer 6 — adaptive AI prompt structure."""

from __future__ import annotations

import random
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest


# ─────────────────────────────────────────────────────────────────────
# parse / get / set
# ─────────────────────────────────────────────────────────────────────

class TestParseLayout:
    def test_empty_returns_empty(self):
        from prompt_layout import parse_layout
        assert parse_layout(None) == {}
        assert parse_layout("") == {}
        assert parse_layout("{}") == {}

    def test_unknown_section_filtered(self):
        from prompt_layout import parse_layout
        out = parse_layout('{"not_a_section": "brief"}')
        assert out == {}

    def test_unknown_verbosity_filtered(self):
        from prompt_layout import parse_layout
        out = parse_layout('{"alt_data": "yolo"}')
        assert out == {}

    def test_normal_stripped_to_keep_sparse(self):
        """Default verbosity 'normal' shouldn't be stored — keep dict
        sparse so common case (no overrides) is empty."""
        from prompt_layout import parse_layout
        out = parse_layout('{"alt_data": "normal"}')
        assert "alt_data" not in out


class TestGetVerbosity:
    def test_default_is_normal(self):
        from prompt_layout import get_verbosity
        profile = {"id": 1}
        assert get_verbosity(profile, "alt_data") == "normal"

    def test_returns_stored_override(self):
        from prompt_layout import get_verbosity
        profile = {"id": 1, "prompt_layout": '{"alt_data": "brief"}'}
        assert get_verbosity(profile, "alt_data") == "brief"


class TestPickRotation:
    def test_picks_a_known_section(self):
        from prompt_layout import pick_rotation, section_names
        rng = random.Random(42)
        section, cur, new = pick_rotation({}, rng=rng)
        assert section in section_names()

    def test_new_verbosity_differs_from_current(self):
        from prompt_layout import pick_rotation
        rng = random.Random(42)
        for _ in range(20):
            section, cur, new = pick_rotation({}, rng=rng)
            assert cur != new


# ─────────────────────────────────────────────────────────────────────
# Cost estimation
# ─────────────────────────────────────────────────────────────────────

class TestCostEstimation:
    def test_normal_to_brief_saves_money(self):
        from prompt_layout import estimate_daily_cost_delta
        delta = estimate_daily_cost_delta("normal", "brief")
        assert delta < 0  # Saves money

    def test_normal_to_detailed_costs_money(self):
        from prompt_layout import estimate_daily_cost_delta
        delta = estimate_daily_cost_delta("normal", "detailed")
        assert delta > 0  # Costs money

    def test_no_change_costs_nothing(self):
        from prompt_layout import estimate_daily_cost_delta
        assert estimate_daily_cost_delta("normal", "normal") == 0


# ─────────────────────────────────────────────────────────────────────
# Tuner — _optimize_prompt_layout
# ─────────────────────────────────────────────────────────────────────

class TestOptimizePromptLayout:
    def test_skips_when_too_few_resolved(self, tmp_path):
        ctx = SimpleNamespace(
            profile_id=1, user_id=1,
            db_path=str(tmp_path / "x.db"),
            prompt_layout="{}",
        )
        from self_tuning import _optimize_prompt_layout
        msg = _optimize_prompt_layout(
            None, ctx, 1, 1, overall_wr=50.0, resolved=10)
        assert msg is None

    def test_skips_when_in_cooldown(self, tmp_path):
        ctx = SimpleNamespace(
            profile_id=1, user_id=1,
            db_path=str(tmp_path / "x.db"),
            prompt_layout="{}",
        )
        from self_tuning import _optimize_prompt_layout
        with patch("self_tuning._get_recent_adjustment",
                    return_value={"id": 1}):
            msg = _optimize_prompt_layout(
                None, ctx, 1, 1, overall_wr=50.0, resolved=100)
            assert msg is None

    def test_rotation_applied_when_within_budget(self, tmp_path):
        ctx = SimpleNamespace(
            profile_id=1, user_id=1,
            db_path=str(tmp_path / "x.db"),
            prompt_layout="{}",
        )
        from self_tuning import _optimize_prompt_layout
        # Force a known rotation that costs money — verify cost-guard
        # check happens.
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("prompt_layout.pick_rotation",
                        return_value=("alt_data", "normal", "detailed")):
                with patch("prompt_layout.set_verbosity") as mock_set:
                    with patch("models.log_tuning_change"):
                        with patch("cost_guard.can_afford_action",
                                    return_value=True):
                            msg = _optimize_prompt_layout(
                                None, ctx, 1, 1,
                                overall_wr=50.0, resolved=100)
                            mock_set.assert_called_with(
                                1, "alt_data", "detailed")
                            assert msg is not None

    def test_cost_gated_when_over_budget(self, tmp_path):
        ctx = SimpleNamespace(
            profile_id=1, user_id=1,
            db_path=str(tmp_path / "x.db"),
            prompt_layout="{}",
        )
        from self_tuning import _optimize_prompt_layout
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("prompt_layout.pick_rotation",
                        return_value=("alt_data", "normal", "detailed")):
                with patch("prompt_layout.set_verbosity") as mock_set:
                    with patch("cost_guard.can_afford_action",
                                return_value=False):
                        with patch("cost_guard.today_spend", return_value=5.0):
                            with patch("cost_guard.daily_ceiling_usd",
                                        return_value=5.0):
                                msg = _optimize_prompt_layout(
                                    None, ctx, 1, 1,
                                    overall_wr=50.0, resolved=100)
                                # Recommendation, not auto-applied
                                mock_set.assert_not_called()
                                assert msg.startswith(
                                    "Recommendation: cost-gated")

    def test_brief_rotation_bypasses_cost_check(self, tmp_path):
        """Cost-saving moves (toward brief) shouldn't be cost-gated."""
        ctx = SimpleNamespace(
            profile_id=1, user_id=1,
            db_path=str(tmp_path / "x.db"),
            prompt_layout="{}",
        )
        from self_tuning import _optimize_prompt_layout
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("prompt_layout.pick_rotation",
                        return_value=("alt_data", "normal", "brief")):
                with patch("prompt_layout.set_verbosity") as mock_set:
                    with patch("models.log_tuning_change"):
                        with patch("cost_guard.can_afford_action") as mock_caa:
                            msg = _optimize_prompt_layout(
                                None, ctx, 1, 1,
                                overall_wr=50.0, resolved=100)
                            # Cost guard NOT consulted (saves money)
                            mock_caa.assert_not_called()
                            mock_set.assert_called_with(
                                1, "alt_data", "brief")


# ─────────────────────────────────────────────────────────────────────
# Prompt builder integration
# ─────────────────────────────────────────────────────────────────────

class TestPromptBuilderRespectsVerbosity:
    def _build_alt_data(self):
        return {
            "insider": {"net_direction": "bullish", "recent_buys": 3, "recent_sells": 0},
            "short": {"short_pct_float": 8, "squeeze_risk": "medium"},
            "options": {"unusual": True, "signal": "bullish", "put_call_ratio": 0.4},
            "intraday": {"vwap_position": "above"},
            "fundamentals": {"pe_trailing": 22.0},
            "insider_cluster": {"is_cluster": True, "insider_count": 5,
                                  "cluster_direction": "buying", "total_value": 1_000_000},
            "analyst_estimates": {"eps_revision_direction": "up", "revision_magnitude_pct": 5},
        }

    def _candidate(self, alt):
        return [{"symbol": "X", "score": 5, "alt_data": alt}]

    def _market_ctx(self):
        return {"regime": "bull", "vix": 15, "spy_trend": "up"}

    def test_alt_data_brief_truncates(self):
        from ai_analyst import _build_batch_prompt
        ctx = SimpleNamespace(
            signal_weights="{}",
            prompt_layout='{"alt_data": "brief"}',
            max_position_pct=0.10, max_total_positions=10,
            enable_short_selling=False, segment="small",
        )
        prompt = _build_batch_prompt(
            self._candidate(self._build_alt_data()),
            {"equity": 100000, "cash": 100000, "positions": [], "num_positions": 0},
            self._market_ctx(), ctx=ctx,
        )
        # Brief mode truncation marker should be present
        assert "more, brief mode" in prompt

    def test_alt_data_normal_shows_all(self):
        from ai_analyst import _build_batch_prompt
        ctx = SimpleNamespace(
            signal_weights="{}",
            prompt_layout="{}",  # normal default
            max_position_pct=0.10, max_total_positions=10,
            enable_short_selling=False, segment="small",
        )
        prompt = _build_batch_prompt(
            self._candidate(self._build_alt_data()),
            {"equity": 100000, "cash": 100000, "positions": [], "num_positions": 0},
            self._market_ctx(), ctx=ctx,
        )
        assert "more, brief mode" not in prompt
