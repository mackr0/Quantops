"""Tests for the pending-orders dashboard feature (Task 18.4, 2026-04-15).

After-hours order submissions queue in Alpaca as `accepted` / `new` and
do not fill until market open. Without surfacing them, the dashboard
was misleading — a user couldn't tell "scheduler has orders waiting"
from "scheduler produced nothing."
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# _safe_pending_orders helper
# ---------------------------------------------------------------------------

class TestSafePendingOrders:
    def _ctx_with_api(self, api):
        return SimpleNamespace(
            get_alpaca_api=lambda: api,
            display_name="Test", segment="small",
        )

    def test_returns_list_of_dicts_shaped_for_ui(self):
        from views import _safe_pending_orders

        fake_api = MagicMock()
        fake_api.list_orders.return_value = [
            SimpleNamespace(
                symbol="AAPL", side="buy", qty="5", order_type="limit",
                limit_price="180.50", status="accepted",
                submitted_at="2026-04-15T20:30:00Z", time_in_force="day",
            ),
        ]
        out = _safe_pending_orders(self._ctx_with_api(fake_api))
        assert len(out) == 1
        o = out[0]
        assert o["symbol"] == "AAPL"
        assert o["side"] == "buy"
        assert o["qty"] == 5.0
        assert o["limit_price"] == 180.50
        assert o["status"] == "accepted"

    def test_market_orders_have_none_limit_price(self):
        from views import _safe_pending_orders

        fake_api = MagicMock()
        fake_api.list_orders.return_value = [
            SimpleNamespace(
                symbol="TSLA", side="sell", qty="10", order_type="market",
                limit_price=None, status="new",
                submitted_at=None, time_in_force="day",
            ),
        ]
        out = _safe_pending_orders(self._ctx_with_api(fake_api))
        assert out[0]["limit_price"] is None
        assert out[0]["order_type"] == "market"

    def test_bad_numeric_fields_do_not_crash(self):
        """Defensive: if Alpaca returns a garbage qty we should still
        produce a row rather than crashing the whole dashboard."""
        from views import _safe_pending_orders

        fake_api = MagicMock()
        fake_api.list_orders.return_value = [
            SimpleNamespace(
                symbol="X", side="buy", qty="not-a-number", order_type="limit",
                limit_price="not-a-price", status="accepted",
                submitted_at=None, time_in_force="gtc",
            ),
        ]
        out = _safe_pending_orders(self._ctx_with_api(fake_api))
        assert len(out) == 1
        assert out[0]["qty"] == 0.0
        assert out[0]["limit_price"] is None

    def test_api_exception_returns_empty_list_not_crash(self):
        from views import _safe_pending_orders

        fake_api = MagicMock()
        fake_api.list_orders.side_effect = RuntimeError("Alpaca 500")
        out = _safe_pending_orders(self._ctx_with_api(fake_api))
        assert out == []

    def test_list_orders_called_with_open_status(self):
        """Filtering to status='open' is the whole point — we don't want
        filled/canceled orders mixing in."""
        from views import _safe_pending_orders

        fake_api = MagicMock()
        fake_api.list_orders.return_value = []
        _safe_pending_orders(self._ctx_with_api(fake_api))
        args, kwargs = fake_api.list_orders.call_args
        assert kwargs.get("status") == "open"
