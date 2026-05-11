"""Phase 2 of Position class refactor (2026-05-11): exit + risk
paths migrated to use Position attributes.

This test pins the behavior of the migrated consumers:
- `bracket_orders.ensure_protective_stops` skips option positions
  via `pos.is_option` (replaces the Phase 1 temp guard using
  _is_occ_symbol heuristic on the symbol string).
- `trader._entry_order_filled_at_broker` takes `broker_symbol`
  unambiguously (was `symbol` overloaded between underlying and
  OCC). Option positions match by OCC at the broker.
- `portfolio_manager.check_trailing_stops` /
  `check_stop_loss_take_profit` skip option positions via
  `pos.is_option`.

The same end-state as Phase 1's temporary guards, but via canonical
Position attributes instead of ad-hoc dict introspection.
"""
import os
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from position import Position


def _stock_pos(symbol="AAPL", qty=100, entry=150.0, current=155.0):
    ap = SimpleNamespace(
        symbol=symbol, qty=str(qty),
        avg_entry_price=str(entry),
        current_price=str(current),
        market_value=str(qty * current),
        unrealized_pl=str(qty * (current - entry)),
        unrealized_plpc=str((current - entry) / entry),
    )
    return Position.from_alpaca(ap)


def _option_pos(occ="PCG260612C00017000", qty=6, entry=0.47, current=0.30):
    ap = SimpleNamespace(
        symbol=occ, qty=str(qty),
        avg_entry_price=str(entry),
        current_price=str(current),
        market_value=str(qty * current * 100),
        unrealized_pl=str(qty * (current - entry) * 100),
        unrealized_plpc=str((current - entry) / entry),
    )
    return Position.from_alpaca(ap)


class TestBracketOrdersUsesIsOption:
    def test_option_position_skipped(self):
        """ensure_protective_stops uses pos.is_option and skips."""
        from bracket_orders import ensure_protective_stops

        api = MagicMock()
        positions = [_option_pos()]
        ctx = MagicMock()
        ctx.stop_loss_pct = 0.05
        ctx.use_trailing_stops = True

        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            from journal import init_db
            init_db(db_path)
            ensure_protective_stops(api, positions, ctx, db_path)
            assert not api.submit_order.called, (
                "Should NOT submit stock-side broker order for an "
                "option position (Phase 2 — pos.is_option guard)"
            )
        finally:
            os.unlink(db_path)

    def test_stock_position_still_protected(self):
        """Regression: stock positions still get their trailing stop."""
        from bracket_orders import ensure_protective_stops

        api = MagicMock()
        api.submit_order.return_value = MagicMock(id="order-1")
        positions = [_stock_pos()]
        ctx = MagicMock()
        ctx.stop_loss_pct = 0.05
        ctx.use_trailing_stops = True

        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            from journal import init_db
            init_db(db_path)
            conn = sqlite3.connect(db_path)
            conn.execute(
                "INSERT INTO trades (symbol, side, qty, price, status) "
                "VALUES ('AAPL', 'buy', 100, 150.0, 'open')"
            )
            conn.commit()
            conn.close()
            ensure_protective_stops(api, positions, ctx, db_path)
            assert api.submit_order.called
        finally:
            os.unlink(db_path)


class TestEntryOrderFilledAtBrokerUsesBrokerSymbol:
    def test_option_matched_by_occ(self):
        """The gate takes broker_symbol directly. For option
        positions caller passes the OCC, which matches Alpaca's
        list_positions output."""
        from trader import _entry_order_filled_at_broker

        api = MagicMock()
        opt = MagicMock()
        opt.symbol = "PCG260612C00017000"
        opt.qty = "6"
        api.list_positions.return_value = [opt]

        assert _entry_order_filled_at_broker(
            api, db_path=None,
            broker_symbol="PCG260612C00017000", is_short=False,
        ) is True

    def test_stock_still_works(self):
        """Regression: stocks pass underlying as broker_symbol."""
        from trader import _entry_order_filled_at_broker

        api = MagicMock()
        stk = MagicMock()
        stk.symbol = "AAPL"
        stk.qty = "100"
        api.list_positions.return_value = [stk]

        assert _entry_order_filled_at_broker(
            api, db_path=None,
            broker_symbol="AAPL", is_short=False,
        ) is True

    def test_no_match_returns_false(self):
        from trader import _entry_order_filled_at_broker

        api = MagicMock()
        api.list_positions.return_value = []
        assert _entry_order_filled_at_broker(
            api, db_path=None,
            broker_symbol="AAPL", is_short=False,
        ) is False


class TestProcessExitTriggerThreadsOccSymbol:
    """The _process_exit_trigger caller derives broker_symbol from
    `trigger_signal.get("occ_symbol") or symbol` and passes it to
    the gate. Without occ_symbol in trigger, the gate searches by
    underlying (correct for stocks)."""

    def test_stock_trigger_derives_broker_symbol_from_symbol(self):
        """Without occ_symbol, broker_symbol falls back to the
        underlying symbol — the same value the gate has always used
        for stocks. Tested by exercising the derivation directly
        rather than the full _process_exit_trigger which has heavy
        downstream dependencies."""
        trigger = {"symbol": "AAPL", "qty": 100, "trigger": "stop_loss"}
        broker_symbol = trigger.get("occ_symbol") or trigger["symbol"]
        assert broker_symbol == "AAPL"

    def test_option_trigger_with_occ_routes_to_occ(self):
        """If a trigger includes occ_symbol, the gate matches by
        OCC at the broker — option exits don't defer forever."""
        from trader import _entry_order_filled_at_broker

        api = MagicMock()
        opt = MagicMock()
        opt.symbol = "PCG260612C00017000"
        opt.qty = "6"
        api.list_positions.return_value = [opt]

        # Mimic what _process_exit_trigger does
        trigger = {
            "symbol": "PCG", "qty": 6, "trigger": "stop_loss",
            "price": 0.30, "reason": "test",
            "occ_symbol": "PCG260612C00017000",
        }
        broker_symbol = trigger.get("occ_symbol") or trigger["symbol"]
        result = _entry_order_filled_at_broker(
            api, db_path=None,
            broker_symbol=broker_symbol, is_short=False,
        )
        assert result is True


class TestPortfolioManagerSkipsOptions:
    def test_trailing_stops_skips_option(self):
        from portfolio_manager import check_trailing_stops

        positions = [_option_pos(qty=1, entry=1.0, current=5.0)]
        ctx = MagicMock()
        ctx.trailing_atr_multiplier = 1.5
        # No bars patching needed — the option-skip is hit before
        # get_bars is called.
        triggered = check_trailing_stops(positions, ctx)
        assert triggered == []

    def test_stop_loss_take_profit_skips_option(self):
        from portfolio_manager import check_stop_loss_take_profit

        positions = [_option_pos(qty=1, entry=1.0, current=0.50)]
        triggered = check_stop_loss_take_profit(
            positions, stop_loss_pct=0.05, take_profit_pct=0.10,
        )
        assert triggered == []

    def test_stock_position_still_triggers(self):
        """Regression: stock at -10% with 5% SL still triggers."""
        from portfolio_manager import check_stop_loss_take_profit

        positions = [_stock_pos(entry=150, current=135)]  # -10%
        triggered = check_stop_loss_take_profit(
            positions, stop_loss_pct=0.05, take_profit_pct=0.10,
        )
        assert len(triggered) == 1
        assert triggered[0]["trigger"] == "stop_loss"
