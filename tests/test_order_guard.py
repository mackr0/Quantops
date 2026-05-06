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
from unittest.mock import MagicMock, patch
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


class TestOvershootGuard:
    """allowable_sell_qty pre-trade guard — caught 2026-05-06 when 31
    phantom broker shorts had accumulated across 3 Alpaca accounts due
    to multi-profile shared-account overshoot. Each profile correctly
    closed its own virtual long; cumulative SELLs at the broker level
    overshot the actual long position, creating shorts no profile
    monitored."""

    def _api(self, positions=None, raise_on_list=False):
        api = MagicMock()
        if raise_on_list:
            api.list_positions.side_effect = RuntimeError("broker down")
        else:
            api.list_positions.return_value = positions or []
        return api

    def _pos(self, symbol, qty):
        p = MagicMock()
        p.symbol = symbol
        p.qty = qty
        return p

    def test_broker_has_enough_returns_full_qty(self):
        from order_guard import allowable_sell_qty
        api = self._api([self._pos("AAPL", "100")])
        qty, reason = allowable_sell_qty(api, "AAPL", 50)
        assert qty == 50
        assert reason == "ok"

    def test_broker_has_zero_refuses_completely(self):
        """Broker has no long shares — submitting would CREATE a short.
        Refuse with allowed_qty=0."""
        from order_guard import allowable_sell_qty
        api = self._api([])
        qty, reason = allowable_sell_qty(api, "BBWI", 187)
        assert qty == 0
        assert "would create short" in reason

    def test_broker_has_some_downsizes(self):
        """Broker has fewer longs than requested — downsize to broker's
        actual qty so the SELL doesn't overshoot."""
        from order_guard import allowable_sell_qty
        api = self._api([self._pos("MSFT", "5")])
        qty, reason = allowable_sell_qty(api, "MSFT", 17)
        assert qty == 5
        assert "downsized" in reason

    def test_broker_short_position_refuses(self):
        """Broker is already net-short the symbol. Submitting a SELL
        would deepen the short — refuse."""
        from order_guard import allowable_sell_qty
        api = self._api([self._pos("BBWI", "-374")])
        qty, reason = allowable_sell_qty(api, "BBWI", 100)
        assert qty == 0
        assert "would create short" in reason

    def test_broker_api_failure_is_permissive(self):
        """If the broker API is down, default permissive — let the
        existing submit_order error handling surface real issues. We
        should never block trading because the GUARD couldn't query."""
        from order_guard import allowable_sell_qty
        api = self._api(raise_on_list=True)
        qty, reason = allowable_sell_qty(api, "AAPL", 10)
        assert qty == 10
        assert "permissive" in reason

    def test_other_symbols_dont_satisfy_check(self):
        """Broker has 100 AAPL but request is for MSFT — refuse for MSFT."""
        from order_guard import allowable_sell_qty
        api = self._api([self._pos("AAPL", "100")])
        qty, reason = allowable_sell_qty(api, "MSFT", 10)
        assert qty == 0

    def test_zero_or_negative_qty_returns_zero(self):
        from order_guard import allowable_sell_qty
        api = self._api([self._pos("AAPL", "100")])
        qty, _ = allowable_sell_qty(api, "AAPL", 0)
        assert qty == 0
        qty, _ = allowable_sell_qty(api, "AAPL", -5)
        assert qty == 0

    def test_options_contract_bypasses_guard(self):
        """Option short legs (covered calls, bull put spreads, iron
        condors) are intentional shorts. The guard would refuse them
        because broker has 0 long of the contract symbol; that's wrong.
        Bypass for OCC-formatted symbols."""
        from order_guard import allowable_sell_qty
        api = self._api([])
        qty, reason = allowable_sell_qty(api, "MSFT260612P00375000", 1)
        assert qty == 1
        assert "option" in reason.lower()

    def test_case_insensitive_symbol_match(self):
        from order_guard import allowable_sell_qty
        api = self._api([self._pos("aapl", "50")])
        qty, _ = allowable_sell_qty(api, "AAPL", 30)
        assert qty == 30
