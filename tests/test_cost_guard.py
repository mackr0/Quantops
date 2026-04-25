"""Tests for cost_guard — daily spend ceiling enforcement."""

from __future__ import annotations

from unittest.mock import patch

import pytest


class TestDailyCeiling:
    def test_floors_at_5_dollars(self):
        from cost_guard import daily_ceiling_usd
        with patch("cost_guard.trailing_avg_daily_spend", return_value=0.0):
            assert daily_ceiling_usd(1) == 5.0

    def test_15x_trailing_avg_above_floor(self):
        from cost_guard import daily_ceiling_usd
        with patch("cost_guard.trailing_avg_daily_spend", return_value=10.0):
            # 10 * 1.5 = 15
            assert daily_ceiling_usd(1) == 15.0

    def test_uses_floor_when_avg_below(self):
        from cost_guard import daily_ceiling_usd
        with patch("cost_guard.trailing_avg_daily_spend", return_value=2.0):
            # 2 * 1.5 = 3, but floor is 5
            assert daily_ceiling_usd(1) == 5.0


class TestCanAffordAction:
    def test_under_ceiling_allowed(self):
        from cost_guard import can_afford_action
        with patch("cost_guard.daily_ceiling_usd", return_value=10.0):
            with patch("cost_guard.today_spend", return_value=3.0):
                assert can_afford_action(1, 0.50)

    def test_over_ceiling_blocked(self):
        from cost_guard import can_afford_action
        with patch("cost_guard.daily_ceiling_usd", return_value=10.0):
            with patch("cost_guard.today_spend", return_value=9.80):
                # 9.80 + 0.50 = 10.30 > 10.0
                assert not can_afford_action(1, 0.50)

    def test_zero_extra_cost_always_allowed_under_ceiling(self):
        from cost_guard import can_afford_action
        with patch("cost_guard.daily_ceiling_usd", return_value=10.0):
            with patch("cost_guard.today_spend", return_value=8.0):
                assert can_afford_action(1, 0.0)

    def test_negative_extra_clamped_to_zero(self):
        from cost_guard import can_afford_action
        # Negative extra cost makes no sense — should be safe to call
        with patch("cost_guard.daily_ceiling_usd", return_value=10.0):
            with patch("cost_guard.today_spend", return_value=5.0):
                assert can_afford_action(1, -100.0)


class TestFormatCostRecommendation:
    def test_starts_with_recommendation_cost_gated(self):
        """Critical: the prefix must be 'Recommendation: cost-gated'
        for the no-recommendation-only guardrail to permit it."""
        from cost_guard import format_cost_recommendation
        with patch("cost_guard.today_spend", return_value=5.0):
            with patch("cost_guard.daily_ceiling_usd", return_value=10.0):
                msg = format_cost_recommendation(
                    "restore signal X", user_id=1,
                    estimated_extra_cost_usd=0.50,
                )
                assert msg.startswith("Recommendation: cost-gated")

    def test_includes_action_summary(self):
        from cost_guard import format_cost_recommendation
        with patch("cost_guard.today_spend", return_value=5.0):
            with patch("cost_guard.daily_ceiling_usd", return_value=10.0):
                msg = format_cost_recommendation(
                    "restore signal X", 1, 0.50)
                assert "restore signal X" in msg

    def test_includes_dollar_amounts(self):
        from cost_guard import format_cost_recommendation
        with patch("cost_guard.today_spend", return_value=5.0):
            with patch("cost_guard.daily_ceiling_usd", return_value=10.0):
                msg = format_cost_recommendation(
                    "restore signal X", 1, 0.50)
                assert "$0.50" in msg
                assert "$5.00" in msg
                assert "$10.00" in msg


class TestStatus:
    def test_returns_full_snapshot(self):
        from cost_guard import status
        with patch("cost_guard.today_spend", return_value=4.0):
            with patch("cost_guard.daily_ceiling_usd", return_value=10.0):
                with patch("cost_guard.trailing_avg_daily_spend", return_value=6.0):
                    s = status(1)
                    assert s["today_usd"] == 4.0
                    assert s["ceiling_usd"] == 10.0
                    assert s["headroom_usd"] == 6.0
                    assert s["trailing_7d_avg_usd"] == 6.0

    def test_headroom_floors_at_zero(self):
        from cost_guard import status
        # Already over the ceiling
        with patch("cost_guard.today_spend", return_value=12.0):
            with patch("cost_guard.daily_ceiling_usd", return_value=10.0):
                with patch("cost_guard.trailing_avg_daily_spend", return_value=6.0):
                    s = status(1)
                    assert s["headroom_usd"] == 0.0
