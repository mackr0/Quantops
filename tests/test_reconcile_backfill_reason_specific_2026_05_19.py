"""Reconcile backfill — pin the specific-protective-order attribution.

Before 2026-05-19 the reconciler labeled every backfilled SELL/COVER
row with the same generic string: 'broker exited via protective
order — backfilled by reconcile'. That hid which protective
mechanism actually fired.

The 2026-05-19 NOW-position incident: 3 profiles held NOW at $103.67
entry; TP=$115.89; SL=$95.52. Actual exit fired at $105.29 because
the trailing stop locked in profit when NOW pulled back from a
$110.83 peak. The operator looking at the journal saw "protective
order" + TP/SL targets that didn't match the exit and reasonably
asked "what the fuck?" The cause was the trailing stop, not the TP
or SL — but the journal didn't say so.

The fix: `_build_backfill_reason` branches on the Alpaca order_type
field that the reconciler already captures, so each row says exactly
which mechanism fired. These tests pin the contract by structural
pattern (substring match for the order kind keyword) so any future
refactor that drops the attribution breaks here.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from reconcile_journal_to_broker import _build_backfill_reason


class TestTrailingStopAttribution:
    """The NOW-incident class. Trailing-stop exits must be named
    explicitly so they aren't confused with TP / SL fires."""

    def test_long_trailing_stop_says_trailing_stop(self):
        reason = _build_backfill_reason(
            order_type="trailing_stop",
            exit_price=105.29, entry_price=103.67,
            side="sell", partial=False,
        )
        assert "trailing stop" in reason.lower()
        # MUST NOT label as generic "protective order"
        assert "protective" not in reason.lower()
        # Move % is helpful context: NOW exit was +1.6% over entry
        assert "+1.6%" in reason
        # Exit price is in the message
        assert "105.29" in reason

    def test_short_trailing_stop_says_trailing_stop(self):
        reason = _build_backfill_reason(
            order_type="trailing_stop",
            exit_price=95.00, entry_price=100.00,
            side="cover", partial=False,
        )
        assert "trailing stop" in reason.lower()
        # Short: profit when cover_price < short_price; 100→95 is +5% gain
        assert "+5.0%" in reason


class TestFixedStopAttribution:
    def test_long_stop_says_stop_loss(self):
        reason = _build_backfill_reason(
            order_type="stop",
            exit_price=95.52, entry_price=103.67,
            side="sell", partial=False,
        )
        assert "stop-loss" in reason.lower()
        # Move was negative — confirms loss
        assert "-7.9%" in reason

    def test_short_stop_says_stop(self):
        reason = _build_backfill_reason(
            order_type="stop",
            exit_price=110.00, entry_price=100.00,
            side="cover", partial=False,
        )
        assert "stop" in reason.lower()


class TestTakeProfitAttribution:
    def test_long_limit_close_says_take_profit(self):
        reason = _build_backfill_reason(
            order_type="limit",
            exit_price=115.89, entry_price=103.67,
            side="sell", partial=False,
        )
        assert "take-profit" in reason.lower()
        assert "+11.8%" in reason


class TestMarketSellNotLabeledProtective:
    """A market sell is almost certainly NOT a protective order — it's
    a manual close or external action. The old code lied by labeling
    it 'protective'; the new code must not."""

    def test_market_sell_labeled_as_manual_or_external(self):
        reason = _build_backfill_reason(
            order_type="market",
            exit_price=100.00, entry_price=100.00,
            side="sell", partial=False,
        )
        assert "manual" in reason.lower() or "external" in reason.lower()
        # The misleading "protective" word must not appear
        assert "protective" not in reason.lower()


class TestPartialFillFlag:
    def test_partial_close_includes_partial_keyword(self):
        reason = _build_backfill_reason(
            order_type="trailing_stop",
            exit_price=105.0, entry_price=100.0,
            side="sell", partial=True,
        )
        assert "partial" in reason.lower()


class TestUnknownOrderTypeFallsBackGracefully:
    """When the broker fill came through without a recognized order_type,
    we still produce SOMETHING readable rather than crashing."""

    def test_none_order_type_uses_generic_fallback(self):
        reason = _build_backfill_reason(
            order_type=None,
            exit_price=100.0, entry_price=100.0,
            side="sell", partial=False,
        )
        # Should still mention the order_type slot ("unknown type")
        assert "unknown" in reason.lower() or "protective" in reason.lower()
        # Must be a non-empty string
        assert reason and len(reason) > 10

    def test_unknown_order_type_string_preserved(self):
        reason = _build_backfill_reason(
            order_type="some_new_alpaca_type",
            exit_price=100.0, entry_price=100.0,
            side="sell", partial=False,
        )
        # Exact order_type string is included so we can debug new
        # broker types if Alpaca adds one.
        assert "some_new_alpaca_type" in reason


class TestMissingPriceDoesNotCrash:
    """`exit_price` and `entry_price` may be None when the reconciler
    can't compute one. Don't crash; just omit the price context."""

    def test_missing_exit_price_skips_price_in_message(self):
        reason = _build_backfill_reason(
            order_type="trailing_stop",
            exit_price=None, entry_price=100.0,
            side="sell", partial=False,
        )
        assert "trailing stop" in reason.lower()
        assert "$" not in reason  # no price was rendered

    def test_zero_entry_price_skips_move_pct(self):
        reason = _build_backfill_reason(
            order_type="trailing_stop",
            exit_price=100.0, entry_price=0,
            side="sell", partial=False,
        )
        assert "trailing stop" in reason.lower()
        # Move pct should be omitted (can't divide by zero)
        assert "%" not in reason


# ---------------------------------------------------------------------------
# Structural test — the lie this incident exposed must remain blocked.
#
# If anyone replaces _build_backfill_reason with a constant or removes
# the order_type branch, the assertion below catches it. The other
# tests above pin specific values; this one pins the BEHAVIORAL
# class (different inputs → distinct outputs).
# ---------------------------------------------------------------------------

class TestOrderTypesProduceDistinctReasons:
    def test_each_protective_order_type_yields_distinct_text(self):
        seen = set()
        for ot in ("trailing_stop", "stop", "stop_limit", "limit", "market"):
            r = _build_backfill_reason(
                order_type=ot,
                exit_price=100.0, entry_price=99.0,
                side="sell", partial=False,
            )
            assert r not in seen, (
                f"order_type={ot!r} produced duplicate reason; the "
                f"differentiation is the whole point of this helper. "
                f"reason was: {r!r}"
            )
            seen.add(r)
