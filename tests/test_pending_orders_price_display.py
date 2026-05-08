"""The pending-orders table on the dashboard must always show
something useful in the Price column — the user's complaint:
trailing-stop rows showed "—" because we only surfaced
`limit_price` from the Alpaca order, ignoring `stop_price`,
`trail_percent`, and `trail_price`.

These tests pin:
1. `_safe_pending_orders` returns the full set of price-related
   fields (limit_price, stop_price, trail_percent, trail_price,
   hwm) for every order, not just limit_price.
2. None of those fields crash on missing attrs (different order
   types have different fields populated).
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


def _make_ctx_with_no_db():
    ctx = MagicMock()
    ctx.db_path = None  # falls back to fail-open (no owned-id filter)
    ctx.display_name = "Test"
    ctx.segment = "test"
    return ctx


def _trailing_stop_order():
    """Mock an Alpaca trailing-stop order. Real broker orders carry
    stop_price (current trigger) + trail_percent (the distance)."""
    o = MagicMock()
    o.id = "trail-1"
    o.symbol = "AAPL"
    o.side = "sell"
    o.qty = "100"
    o.order_type = "trailing_stop"
    o.limit_price = None
    o.stop_price = "172.50"
    o.trail_percent = "3.0"
    o.trail_price = None
    o.hwm = "178.00"
    o.status = "new"
    o.submitted_at = "2026-05-07T19:25:56"
    o.time_in_force = "gtc"
    return o


def _limit_order():
    o = MagicMock()
    o.id = "limit-1"
    o.symbol = "MSFT"
    o.side = "buy"
    o.qty = "10"
    o.order_type = "limit"
    o.limit_price = "395.00"
    o.stop_price = None
    o.trail_percent = None
    o.trail_price = None
    o.hwm = None
    o.status = "new"
    o.submitted_at = "2026-05-07T20:00:00"
    o.time_in_force = "day"
    return o


def _stop_order():
    o = MagicMock()
    o.id = "stop-1"
    o.symbol = "TSLA"
    o.side = "sell"
    o.qty = "50"
    o.order_type = "stop"
    o.limit_price = None
    o.stop_price = "245.00"
    o.trail_percent = None
    o.trail_price = None
    o.hwm = None
    o.status = "new"
    o.submitted_at = "2026-05-07T20:00:00"
    o.time_in_force = "gtc"
    return o


class TestPendingOrdersPricing:
    def test_trailing_stop_returns_stop_price_and_trail_percent(self):
        from views import _safe_pending_orders

        api = MagicMock()
        api.list_orders.return_value = [_trailing_stop_order()]
        ctx = _make_ctx_with_no_db()
        ctx.get_alpaca_api = lambda: api

        result = _safe_pending_orders(ctx)
        assert len(result) == 1
        o = result[0]
        # Without these, the dashboard showed "—" — the user's
        # complaint. The current stop trigger MUST be exposed.
        assert o["stop_price"] == 172.50
        assert o["trail_percent"] == 3.0
        assert o["limit_price"] is None  # no limit on a trailing stop
        assert o["hwm"] == 178.00

    def test_limit_order_returns_limit_price(self):
        from views import _safe_pending_orders

        api = MagicMock()
        api.list_orders.return_value = [_limit_order()]
        ctx = _make_ctx_with_no_db()
        ctx.get_alpaca_api = lambda: api

        result = _safe_pending_orders(ctx)
        assert len(result) == 1
        o = result[0]
        assert o["limit_price"] == 395.00
        # Stop fields stay None on a limit order
        assert o["stop_price"] is None
        assert o["trail_percent"] is None

    def test_stop_order_returns_stop_price(self):
        from views import _safe_pending_orders

        api = MagicMock()
        api.list_orders.return_value = [_stop_order()]
        ctx = _make_ctx_with_no_db()
        ctx.get_alpaca_api = lambda: api

        result = _safe_pending_orders(ctx)
        o = result[0]
        assert o["stop_price"] == 245.00
        assert o["limit_price"] is None
        assert o["trail_percent"] is None

    def test_missing_fields_dont_crash(self):
        """Some Alpaca order types don't carry every field. Missing
        attrs must produce None, not raise."""
        from views import _safe_pending_orders

        # Bare order with only the absolute-minimum fields. trail_*,
        # stop_price, hwm, even limit_price are absent on the mock.
        bare = MagicMock(spec=["id", "symbol", "side", "qty",
                                "order_type", "status",
                                "submitted_at", "time_in_force"])
        bare.id = "bare-1"
        bare.symbol = "X"
        bare.side = "buy"
        bare.qty = "1"
        bare.order_type = "market"
        bare.status = "new"
        bare.submitted_at = "2026-05-07T20:00:00"
        bare.time_in_force = "day"

        api = MagicMock()
        api.list_orders.return_value = [bare]
        ctx = _make_ctx_with_no_db()
        ctx.get_alpaca_api = lambda: api

        result = _safe_pending_orders(ctx)
        assert len(result) == 1
        o = result[0]
        # Every price field present, all None
        for field in ("limit_price", "stop_price", "trail_percent",
                      "trail_price", "hwm"):
            assert field in o
            assert o[field] is None

    def test_template_logic_picks_right_price_per_order_type(self):
        """The dashboard template (_trades_table doesn't render
        pending orders; dashboard.html does) shows:
        - limit orders: limit_price
        - stop orders: stop_price
        - trailing stops: stop_price + trail %
        Verified via the data shape that supports each branch."""
        from views import _safe_pending_orders

        api = MagicMock()
        api.list_orders.return_value = [
            _limit_order(), _stop_order(), _trailing_stop_order(),
        ]
        ctx = _make_ctx_with_no_db()
        ctx.get_alpaca_api = lambda: api

        result = _safe_pending_orders(ctx)
        # All three orders preserve at least one price-shaped field
        for o in result:
            has_a_price = (
                o["limit_price"] is not None
                or o["stop_price"] is not None
                or o["trail_percent"] is not None
                or o["trail_price"] is not None
            )
            assert has_a_price, (
                f"Order {o['symbol']} ({o['order_type']}) has no "
                f"price-shaped field — dashboard would show '—'"
            )
