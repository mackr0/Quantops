"""Structural guardrail: the multileg IV-rich/IV-cheap thresholds
must maintain a NEUTRAL DEAD ZONE of at least 10 points so that
candidates with mid-range IV don't always receive a pre-built
options recommendation.

The bug class (2026-05-14 incident).
On 2026-05-12 the dead zone was deliberately removed (rich=55,
cheap=55) to "double the proposal funnel". Side effect: every
candidate with IV data received a pre-built multileg
recommendation in the AI prompt. The AI, faced with a
pre-analyzed options strategy next to a bare stock candidate,
picked the options strategy nearly every time. Stock BUY signals
fell from ~24/day to 0/day over the next two weeks.

Restoring the dead zone (rich=60, cheap=45, 15-point neutral band)
ensures that mid-range-IV candidates have no options recommendation
attached, so the AI evaluates them as stock opportunities or skips.

Per-profile overrides are allowed via `option_iv_rich_threshold` /
`option_iv_cheap_threshold` columns on trading_profiles, but the
gap between them must remain at least MIN_DEAD_ZONE_POINTS.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


MIN_DEAD_ZONE_POINTS = 10
"""Smallest acceptable gap between IV-rich and IV-cheap thresholds.
Below this, the AI prompt fills with multileg recommendations on
nearly every candidate and stock-action signals collapse."""


class TestMultilegIvDeadZone:
    def test_module_defaults_have_dead_zone(self):
        """The default constants in options_strategy_advisor must
        maintain at least MIN_DEAD_ZONE_POINTS between rich and cheap."""
        from options_strategy_advisor import (
            MULTILEG_IV_CHEAP_THRESHOLD,
            MULTILEG_IV_RICH_THRESHOLD,
        )
        gap = MULTILEG_IV_RICH_THRESHOLD - MULTILEG_IV_CHEAP_THRESHOLD
        assert MULTILEG_IV_RICH_THRESHOLD > MULTILEG_IV_CHEAP_THRESHOLD, (
            f"MULTILEG_IV_RICH_THRESHOLD ({MULTILEG_IV_RICH_THRESHOLD}) "
            f"must be strictly greater than MULTILEG_IV_CHEAP_THRESHOLD "
            f"({MULTILEG_IV_CHEAP_THRESHOLD}). Without a dead zone, "
            f"every candidate gets a multileg recommendation and stock "
            f"BUY signals collapse (the 2026-05-12 → 2026-05-14 bug)."
        )
        assert gap >= MIN_DEAD_ZONE_POINTS, (
            f"Dead zone is only {gap:.1f} points "
            f"(rich={MULTILEG_IV_RICH_THRESHOLD}, "
            f"cheap={MULTILEG_IV_CHEAP_THRESHOLD}). Minimum required "
            f"is {MIN_DEAD_ZONE_POINTS}. Without sufficient dead zone, "
            f"the AI prompt fills with pre-built multileg "
            f"recommendations on nearly every candidate and stock "
            f"action signals get crowded out."
        )

    def test_neutral_iv_emits_no_multileg_rec(self):
        """A candidate with IV rank squarely inside the dead zone must
        receive NO multileg recommendation. This is the behavioral
        guarantee that the prompt asymmetry is bounded."""
        from options_strategy_advisor import (
            MULTILEG_IV_CHEAP_THRESHOLD,
            MULTILEG_IV_RICH_THRESHOLD,
            evaluate_candidate_for_multileg,
        )
        neutral_iv = (
            (MULTILEG_IV_RICH_THRESHOLD + MULTILEG_IV_CHEAP_THRESHOLD) / 2
        )
        bullish_candidate = {
            "symbol": "AAPL",
            "signal": "BUY",
            "price": 180.0,
        }
        recs = evaluate_candidate_for_multileg(
            bullish_candidate, iv_rank_pct=neutral_iv, regime="trending",
        )
        assert recs == [], (
            f"Bullish candidate with neutral IV ({neutral_iv:.0f}) "
            f"produced multileg recommendations: {recs}. "
            f"Expected empty list — neutral IV should not trigger "
            f"pre-built options recommendations or the AI will pick "
            f"options over a bare stock BUY."
        )
        bearish_candidate = {
            "symbol": "JPM",
            "signal": "SHORT",
            "price": 150.0,
        }
        recs = evaluate_candidate_for_multileg(
            bearish_candidate, iv_rank_pct=neutral_iv, regime="trending",
        )
        assert recs == [], (
            f"Bearish candidate with neutral IV ({neutral_iv:.0f}) "
            f"produced multileg recommendations: {recs}. "
            f"Expected empty list."
        )

    def test_rich_iv_still_fires_credit_spread(self):
        """Sanity: above the rich threshold, bullish candidates still
        receive bull_put_spread recommendations (the credit-side
        opportunity isn't broken by the dead zone)."""
        from options_strategy_advisor import (
            MULTILEG_IV_RICH_THRESHOLD,
            evaluate_candidate_for_multileg,
        )
        rich_iv = MULTILEG_IV_RICH_THRESHOLD + 5
        candidate = {
            "symbol": "AAPL",
            "signal": "BUY",
            "price": 180.0,
        }
        recs = evaluate_candidate_for_multileg(
            candidate, iv_rank_pct=rich_iv, regime="trending",
        )
        strategies = {r["strategy"] for r in recs}
        assert "bull_put_spread" in strategies, (
            f"Bullish candidate with rich IV ({rich_iv:.0f}) did not "
            f"produce a bull_put_spread. Got: {strategies}"
        )

    def test_cheap_iv_still_fires_debit_spread(self):
        """Sanity: below the cheap threshold, bullish candidates still
        receive bull_call_spread recommendations."""
        from options_strategy_advisor import (
            MULTILEG_IV_CHEAP_THRESHOLD,
            evaluate_candidate_for_multileg,
        )
        cheap_iv = max(0, MULTILEG_IV_CHEAP_THRESHOLD - 5)
        candidate = {
            "symbol": "AAPL",
            "signal": "BUY",
            "price": 180.0,
        }
        recs = evaluate_candidate_for_multileg(
            candidate, iv_rank_pct=cheap_iv, regime="trending",
        )
        strategies = {r["strategy"] for r in recs}
        assert "bull_call_spread" in strategies, (
            f"Bullish candidate with cheap IV ({cheap_iv:.0f}) did not "
            f"produce a bull_call_spread. Got: {strategies}"
        )
