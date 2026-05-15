"""Structural guardrail: `check_stop_loss_take_profit` must NEVER
fire a stop-loss SELL on a position that's almost-certainly an
option leg in disguise.

The bug class (2026-05-11 incident).
A multileg combo's leg position came through `get_virtual_positions`
(or some other position-fetching path) with `occ_symbol` accidentally
empty. The check_stop_loss_take_profit function correctly skips on
`occ_symbol` set, but with the field empty it processed the leg as
a stock — saw current_price=$0.16 vs avg_entry=$0.20, calculated
-20% drop, fired stop-loss SELL against the underlying symbol. 37
such SELLs got submitted to the broker before the upstream Phase 5e
fixes plugged the propagation gap.

The fix that landed in 2026-05-12-ish closed the upstream hole. This
test pins a defensive backstop here in `check_stop_loss_take_profit`
itself: positions with both prices under $20 AND a >30% drop are
treated as suspect option legs and skipped (with a warning log) even
when `occ_symbol` is absent. Real stocks rarely drop 30% intraday
without circuit breakers; the false-positive on legitimate penny-stock
crashes is acceptable since the operator can manually trigger the exit.

This test ALSO catches any future refactor that removes the
defensive check.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


def _make_pos(symbol, current, entry, qty=1, occ_symbol=None,
              is_option=False):
    return {
        "symbol": symbol,
        "current_price": current,
        "avg_entry_price": entry,
        "qty": qty,
        "occ_symbol": occ_symbol,
        "is_option": is_option,
    }


class TestNoOptionLegStockSells:
    def test_suspect_option_leg_with_no_occ_symbol_does_not_trigger(self):
        """The bug shape from 2026-05-11: $0.16 current, $0.20 entry,
        no occ_symbol set. Must NOT trigger a stop-loss SELL even
        though the percent drop crosses the stock threshold."""
        from portfolio_manager import check_stop_loss_take_profit
        positions = [_make_pos("KO", current=0.16, entry=0.20)]
        triggered = check_stop_loss_take_profit(
            positions, stop_loss_pct=0.05,
        )
        assert triggered == [], (
            f"Position with both prices < $20 AND a >20% drop must "
            f"be treated as a suspect option leg and skipped (the "
            f"2026-05-11 bug shape). Got: {triggered}"
        )

    def test_legitimate_stock_drop_still_triggers(self):
        """A stock with normal pricing that has a 6% drop must
        still fire the stop-loss. The defensive check shouldn't
        false-positive on real stock action."""
        from portfolio_manager import check_stop_loss_take_profit
        positions = [_make_pos("AAPL", current=181.50, entry=193.00)]
        triggered = check_stop_loss_take_profit(
            positions, stop_loss_pct=0.05,
        )
        assert len(triggered) == 1, (
            f"Real stock with -6% drop must trigger stop-loss; "
            f"got: {triggered}"
        )
        assert triggered[0]["symbol"] == "AAPL"
        assert triggered[0]["trigger"] == "stop_loss"

    def test_explicit_option_position_still_skipped(self):
        """The pre-existing skip on `occ_symbol` set / `is_option`
        True must still work — defensive check is additional, not
        replacement."""
        from portfolio_manager import check_stop_loss_take_profit
        # An option with occ_symbol set: skipped by the original
        # check, never reaches the new defensive check.
        positions = [_make_pos(
            "AAPL", current=0.16, entry=0.20,
            occ_symbol="AAPL260618C00310000",
        )]
        triggered = check_stop_loss_take_profit(
            positions, stop_loss_pct=0.05,
        )
        assert triggered == []

    def test_high_priced_stock_with_huge_drop_still_triggers(self):
        """A real stock at $100 dropping to $50 (-50%) must STILL
        trigger — both prices are above $20, so the suspect-leg
        heuristic doesn't fire. Real catastrophic stock moves
        deserve the safety stop."""
        from portfolio_manager import check_stop_loss_take_profit
        positions = [_make_pos("XYZ", current=50.0, entry=100.0)]
        triggered = check_stop_loss_take_profit(
            positions, stop_loss_pct=0.05,
        )
        assert len(triggered) == 1, (
            f"Real stock with $100 → $50 drop must trigger; "
            f"both prices above $20 means the heuristic doesn't "
            f"protect. Got: {triggered}"
        )

    def test_low_priced_stock_with_modest_drop_still_triggers(self):
        """A penny stock at $5 dropping to $4.70 (-6%) must trigger.
        Both prices are above $2 so the heuristic passes through
        and the normal stop-loss fires."""
        from portfolio_manager import check_stop_loss_take_profit
        positions = [_make_pos("PENNY", current=4.70, entry=5.00)]
        triggered = check_stop_loss_take_profit(
            positions, stop_loss_pct=0.05,
        )
        assert len(triggered) == 1, (
            f"Real penny stock with -6% drop must trigger; only "
            f"sub-$2 prices fall under the option-leg heuristic. "
            f"Got: {triggered}"
        )

    def test_sub_two_dollar_stock_with_huge_drop_still_caught(self):
        """A position priced under $2 with a meaningful drop is
        suspect — real positions priced this low almost certainly
        come from an option leg in this system (min_price=$1
        default keeps stock entries above $1 and well above $2 for
        most segments). False-positives on legitimate sub-$2 stock
        crashes are an acceptable trade for stopping the bug class."""
        from portfolio_manager import check_stop_loss_take_profit
        positions = [_make_pos("SUBPENNY", current=1.50, entry=1.80)]
        triggered = check_stop_loss_take_profit(
            positions, stop_loss_pct=0.05,
        )
        assert triggered == [], (
            f"Sub-$2 position with -16% drop must be skipped per "
            f"the defensive guardrail. Got: {triggered}"
        )

    def test_warning_logged_when_skipped(self, caplog):
        """When the defensive check fires, it must log a WARNING
        with enough detail for an operator to review and manually
        trigger the exit if it was a legitimate penny-stock crash."""
        import logging
        from portfolio_manager import check_stop_loss_take_profit
        caplog.set_level(logging.WARNING, logger="portfolio_manager")
        positions = [_make_pos("KO", current=0.16, entry=0.20)]
        check_stop_loss_take_profit(
            positions, stop_loss_pct=0.05,
        )
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any(
            "option leg" in r.message.lower() for r in warnings
        ), (
            f"Expected a 'suspect option leg' warning when the check "
            f"fired. Got: {[r.message for r in warnings]}"
        )
