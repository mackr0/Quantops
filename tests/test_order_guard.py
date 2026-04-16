"""Tests for `order_guard.check_can_submit` (2026-04-15).

Every order submission must pass through this guard. The bug:
a scan cycle starts at 3:50 PM ET (within market_hours), but
the pipeline takes 80+ minutes and the actual order submission
lands at 5:10 PM ET — after hours. Alpaca paper trading fills
it, producing an accidental after-hours trade.

The guard checks `is_within_schedule` at order time (not cycle
start time) and blocks if outside the window.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

_ET = ZoneInfo("America/New_York")


def _ctx(schedule_type="market_hours"):
    from user_context import UserContext
    return UserContext(
        user_id=1, segment="small", display_name="Test",
        alpaca_api_key="k", alpaca_secret_key="s",
        ai_provider="anthropic", ai_model="claude-haiku-4-5-20251001",
        ai_api_key="k", db_path=":memory:",
        schedule_type=schedule_type,
    )


class TestMarketHoursProfile:
    def test_allows_order_during_market_hours(self):
        from order_guard import check_can_submit
        # Wednesday 10:30 AM ET
        fake_now = datetime(2026, 4, 15, 10, 30, tzinfo=_ET)
        with patch("order_guard.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            assert check_can_submit(_ctx(), "AAPL", "buy") is True

    def test_blocks_order_after_market_close(self):
        from order_guard import check_can_submit
        # Wednesday 5:10 PM ET — the ALM bug
        fake_now = datetime(2026, 4, 15, 17, 10, tzinfo=_ET)
        with patch("order_guard.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            assert check_can_submit(_ctx(), "ALM", "buy") is False

    def test_blocks_order_before_market_open(self):
        from order_guard import check_can_submit
        fake_now = datetime(2026, 4, 15, 8, 0, tzinfo=_ET)
        with patch("order_guard.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            assert check_can_submit(_ctx(), "AAPL", "buy") is False

    def test_blocks_on_weekend(self):
        from order_guard import check_can_submit
        # Saturday 11 AM ET
        fake_now = datetime(2026, 4, 18, 11, 0, tzinfo=_ET)
        with patch("order_guard.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            assert check_can_submit(_ctx(), "AAPL", "sell") is False


class TestExtendedHoursProfile:
    def test_allows_order_at_5pm_et(self):
        """Extended hours: 4 AM - 8 PM ET. 5:10 PM is fine."""
        from order_guard import check_can_submit
        fake_now = datetime(2026, 4, 15, 17, 10, tzinfo=_ET)
        with patch("order_guard.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            assert check_can_submit(_ctx("extended_hours"), "AAPL", "buy") is True

    def test_blocks_order_at_9pm_et(self):
        from order_guard import check_can_submit
        fake_now = datetime(2026, 4, 15, 21, 0, tzinfo=_ET)
        with patch("order_guard.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            assert check_can_submit(_ctx("extended_hours"), "AAPL", "buy") is False


class TestTwentyFourSevenProfile:
    def test_allows_order_anytime(self):
        from order_guard import check_can_submit
        # Saturday 3 AM
        fake_now = datetime(2026, 4, 18, 3, 0, tzinfo=_ET)
        with patch("order_guard.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            assert check_can_submit(_ctx("24_7"), "BTC/USD", "buy") is True


class TestNoContext:
    def test_allows_order_when_no_ctx(self):
        """Legacy code paths that don't pass ctx should not crash."""
        from order_guard import check_can_submit
        assert check_can_submit(None, "AAPL", "buy") is True


class TestBothSidesGuarded:
    def test_buy_blocked(self):
        from order_guard import check_can_submit
        fake_now = datetime(2026, 4, 15, 17, 10, tzinfo=_ET)
        with patch("order_guard.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            assert check_can_submit(_ctx(), "AAPL", "buy") is False

    def test_sell_blocked(self):
        from order_guard import check_can_submit
        fake_now = datetime(2026, 4, 15, 17, 10, tzinfo=_ET)
        with patch("order_guard.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            assert check_can_submit(_ctx(), "AAPL", "sell") is False
