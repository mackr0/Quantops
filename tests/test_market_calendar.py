"""Holiday-aware market gating (market_calendar).

Regression for: schedulers + per-profile schedule checks decided
"is the market open?" from weekday + clock time alone, so they ran
full scan/trade cycles on market holidays (e.g. Memorial Day) and
submitted orders that filled at the next session's open.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import market_calendar as mc

ET = ZoneInfo("America/New_York")

# 2026 anchors
MEMORIAL_DAY = datetime(2026, 5, 25, 11, 0, tzinfo=ET)   # Mon holiday
NORMAL_MON = datetime(2026, 5, 18, 10, 30, tzinfo=ET)    # Mon trading day
NORMAL_MON_AFTER = datetime(2026, 5, 18, 18, 0, tzinfo=ET)  # after close
SATURDAY = datetime(2026, 5, 23, 12, 0, tzinfo=ET)       # weekend
SUNDAY_BEFORE_MEMORIAL = datetime(2026, 5, 24, 12, 0, tzinfo=ET)


@pytest.fixture(autouse=True)
def _reset_and_force_fallback(monkeypatch):
    """Reset caches and (by default) force the deterministic fallback by
    making the live Alpaca paths return None. Tests that want the live
    path re-patch these."""
    mc._clock_cache.update({"ts": 0.0, "val": None})
    mc._cal_cache.clear()
    monkeypatch.setattr(mc, "_get_clock", lambda: None)
    monkeypatch.setattr(mc, "_trading_day_live", lambda: None)


class TestFallbackHolidays:
    def test_memorial_day_is_closed(self):
        assert mc.is_market_open(MEMORIAL_DAY) is False
        assert mc.is_trading_day(MEMORIAL_DAY) is False
        assert mc.is_market_holiday(MEMORIAL_DAY) is True

    def test_normal_weekday_open_during_hours(self):
        assert mc.is_market_open(NORMAL_MON) is True
        assert mc.is_trading_day(NORMAL_MON) is True
        assert mc.is_market_holiday(NORMAL_MON) is False

    def test_normal_weekday_after_close(self):
        assert mc.is_market_open(NORMAL_MON_AFTER) is False
        # still a trading day, just outside the session
        assert mc.is_trading_day(NORMAL_MON_AFTER) is True

    def test_weekend_closed_but_not_holiday(self):
        assert mc.is_market_open(SATURDAY) is False
        assert mc.is_trading_day(SATURDAY) is False
        # weekend is not a "holiday" — keeps weekend-inclusive custom
        # schedules working
        assert mc.is_market_holiday(SATURDAY) is False

    def test_next_open_skips_holiday(self):
        # Sunday before Memorial Day -> next open is Tuesday 9:30, not
        # Monday (the holiday).
        nxt = mc.next_market_open(SUNDAY_BEFORE_MEMORIAL)
        assert (nxt.year, nxt.month, nxt.day) == (2026, 5, 26)
        assert (nxt.hour, nxt.minute) == (9, 30)


class TestLivePath:
    def test_live_clock_drives_is_market_open(self, monkeypatch):
        class _Clock:
            is_open = True
        monkeypatch.setattr(mc, "_get_clock", lambda: _Clock())
        # now=None is "live" -> consults the (mocked) clock
        assert mc.is_market_open(None) is True
        _Clock.is_open = False
        assert mc.is_market_open(None) is False

    def test_live_calendar_drives_trading_day(self, monkeypatch):
        monkeypatch.setattr(mc, "_trading_day_live", lambda: False)
        assert mc.is_trading_day(None) is False
        monkeypatch.setattr(mc, "_trading_day_live", lambda: True)
        assert mc.is_trading_day(None) is True


class TestIsWithinScheduleHoliday:
    """The actual production bug: market_hours profiles ran on holidays."""

    def _ctx(self, schedule_type="market_hours", **kw):
        from user_context import UserContext
        return UserContext(
            user_id=1, segment="test",
            alpaca_api_key="k", alpaca_secret_key="s",
            schedule_type=schedule_type, **kw,
        )

    def test_market_hours_inactive_on_holiday(self):
        ctx = self._ctx("market_hours")
        assert ctx.is_within_schedule(MEMORIAL_DAY) is False

    def test_market_hours_active_on_normal_day(self):
        ctx = self._ctx("market_hours")
        assert ctx.is_within_schedule(NORMAL_MON) is True

    def test_extended_hours_inactive_on_holiday(self):
        ctx = self._ctx("extended_hours")
        # 11:00 on a holiday — would be inside the 4:00-20:00 window
        assert ctx.is_within_schedule(MEMORIAL_DAY) is False

    def test_24_7_active_on_holiday(self):
        ctx = self._ctx("24_7")
        assert ctx.is_within_schedule(MEMORIAL_DAY) is True
