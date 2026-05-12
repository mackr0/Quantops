"""Option-only metrics aggregations.

Phase 1 of the instrument-class pipeline refactor (2026-05-11).
See `docs/14_INSTRUMENT_PIPELINE_ARCHITECTURE.md`.

Every aggregate here filters `WHERE occ_symbol IS NOT NULL`. CRUCIAL
contract: this module never reports slippage as a percentage of
premium. Option premiums are pennies; a 5¢ → 50¢ mark swing is a
900% "slippage" by stock-style math but ~$0.45 in actual cost. We
report option slippage in DOLLARS only.

Used by `pipelines.option.OptionPipeline.compute_metrics()`.
"""
from __future__ import annotations

from typing import Optional

from journal import get_slippage_stats as _journal_slippage_stats


def slippage_stats(db_path: str) -> Optional[dict]:
    """Option-only slippage stats — CASH ONLY.

    Filters `WHERE occ_symbol IS NOT NULL`. The returned dict's
    `avg_slippage_pct` and `worst_slippage_pct` are EXPLICITLY SET
    TO None so consumers can't accidentally render a percentage
    that's nonsense (a 5¢→50¢ mark swing is 900% by the formula
    but ~$0.45 cost; the % is a misleading metric for options).

    Dollar-denominated fields are kept (`total_slippage_cost`,
    `total_slippage_magnitude`, `trades_with_fills`) because they're
    actually meaningful — execution cost in dollars is comparable
    across penny-premium options and dollar-priced stocks.

    For options, also multiplies the dollar fields by the contract
    multiplier (100 shares per contract) so the dollar value
    represents the actual portfolio impact of the slippage rather
    than just the per-share premium delta.

    Returns None when there are no option trades with fill data.
    """
    raw = _journal_slippage_stats(db_path, kind="options")
    if not raw:
        return None
    # Apply contract multiplier on dollar fields. The journal's
    # `qty` column for option rows is in CONTRACTS; the slippage
    # SQL multiplies (fill_price - decision_price) * qty which gives
    # premium-deltas-per-share-times-contracts. Multiply by 100 to
    # get actual dollars.
    return {
        "trades_with_fills": raw["trades_with_fills"],
        # NEVER expose the % aggregates for options — they're
        # mathematically valid but practically misleading on penny
        # premiums.
        "avg_slippage_pct": None,
        "worst_slippage_pct": None,
        "total_slippage_cost": round(
            (raw.get("total_slippage_cost") or 0) * 100, 2
        ),
        "total_slippage_magnitude": round(
            (raw.get("total_slippage_magnitude") or 0) * 100, 2
        ),
        "worst_trade": raw.get("worst_trade"),
    }


# Phase 1 deliberately ships only the slippage helper — closing
# TODO #8 / audit finding #1. Subsequent commits will add:
#   - theta_decay_adjusted_return(): subtract expected theta decay
#     from realized P&L before rolling into win/loss buckets, so
#     "earned the premium" is distinguished from "underlying moved
#     in our favor."
#   - gamma_exposure(): sum signed gamma across all open option
#     positions for the AI prompt's risk summary.
#   - iv_rank_bucketed_pnl(): P&L grouped by entry IV rank, so the
#     tuner can see "we make money when IV rank is in 60-80, lose
#     when 0-20."
