"""Test UserContext dataclass and schedule logic."""

import pytest
from datetime import datetime
from zoneinfo import ZoneInfo


class TestUserContext:
    def test_create_minimal(self):
        from user_context import UserContext
        ctx = UserContext(
            user_id=1,
            segment="small",
            alpaca_api_key="key",
            alpaca_secret_key="secret",
        )
        assert ctx.user_id == 1
        assert ctx.segment == "small"
        assert ctx.stop_loss_pct == 0.03  # default
        assert ctx.max_total_positions == 10  # default

    def test_defaults_are_sane(self):
        from user_context import UserContext
        ctx = UserContext(
            user_id=1,
            segment="test",
            alpaca_api_key="k",
            alpaca_secret_key="s",
        )
        assert 0 < ctx.stop_loss_pct < 1
        assert 0 < ctx.take_profit_pct < 1
        assert 0 < ctx.max_position_pct < 1
        assert ctx.max_total_positions > 0
        assert ctx.ai_confidence_threshold >= 0
        assert ctx.drawdown_pause_pct > ctx.drawdown_reduce_pct

    def test_schedule_type_default(self):
        from user_context import UserContext
        ctx = UserContext(
            user_id=1, segment="test",
            alpaca_api_key="k", alpaca_secret_key="s",
        )
        assert ctx.schedule_type == "market_hours"

    def test_24_7_always_active(self):
        from user_context import UserContext
        ctx = UserContext(
            user_id=1, segment="crypto",
            alpaca_api_key="k", alpaca_secret_key="s",
            schedule_type="24_7",
        )
        # Sunday 3 AM should still be active for 24/7
        sunday_3am = datetime(2026, 4, 12, 3, 0, tzinfo=ZoneInfo("US/Eastern"))
        assert ctx.is_within_schedule(sunday_3am) is True

    def test_market_hours_weekday(self):
        from user_context import UserContext
        ctx = UserContext(
            user_id=1, segment="test",
            alpaca_api_key="k", alpaca_secret_key="s",
            schedule_type="market_hours",
        )
        # Monday 10:30 AM ET should be active
        mon_1030 = datetime(2026, 4, 13, 10, 30, tzinfo=ZoneInfo("US/Eastern"))
        assert ctx.is_within_schedule(mon_1030) is True

    def test_market_hours_weekend(self):
        from user_context import UserContext
        ctx = UserContext(
            user_id=1, segment="test",
            alpaca_api_key="k", alpaca_secret_key="s",
            schedule_type="market_hours",
        )
        # Sunday should be inactive
        sunday = datetime(2026, 4, 12, 12, 0, tzinfo=ZoneInfo("US/Eastern"))
        assert ctx.is_within_schedule(sunday) is False

    def test_market_hours_after_close(self):
        from user_context import UserContext
        ctx = UserContext(
            user_id=1, segment="test",
            alpaca_api_key="k", alpaca_secret_key="s",
            schedule_type="market_hours",
        )
        # Monday 5 PM ET should be inactive
        mon_5pm = datetime(2026, 4, 13, 17, 0, tzinfo=ZoneInfo("US/Eastern"))
        assert ctx.is_within_schedule(mon_5pm) is False

    def test_build_from_segment(self):
        from user_context import build_context_from_segment
        ctx = build_context_from_segment("small")
        assert ctx.segment == "small"
        assert ctx.min_price == 5.0
        assert ctx.max_price == 20.0

    def test_all_segment_types(self):
        from user_context import build_context_from_segment
        for seg in ["micro", "small", "midcap", "largecap", "crypto"]:
            ctx = build_context_from_segment(seg)
            assert ctx.segment == seg
