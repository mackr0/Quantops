"""analyst_upgrade_drift — follow the 2–3 day drift after a fresh
analyst-consensus shift.

Sell-side analyst revisions are followed by predictable short-term
drift in the direction of the revision (Womack 1996, Jegadeesh et al.
2004). The effect lives roughly 2–5 trading days before being absorbed.

Original implementation (pre-2026-05-15) read individual `To Grade` /
`From Grade` columns from yfinance. yfinance changed schema; those
columns no longer exist. The endpoint now returns AGGREGATE
distributions per period (`strongBuy`, `buy`, `hold`, `sell`,
`strongSell` counts per month), so we detect a "revision" as a
period-over-period shift in the weighted-mean rating. Implementation
encapsulated in `analyst_data.recommendation_shift` (yfinance
grandfathered there since no Alpaca / custom altdata source exists
for this).
"""

from __future__ import annotations


import logging

logger = logging.getLogger(__name__)

from typing import Any, Dict, List


NAME = "analyst_upgrade_drift"
APPLICABLE_MARKETS = ["stocks"]
# Sources alternative data (analyst-consensus revisions — part of the
# same alt-data block, `analyst_estimates`, that enable_alt_data gates
# in the AI prompt). Gated out of the candidate pool for the NoAltData
# ablation arm — see insider_cluster.
USES_ALT_DATA = True


def find_candidates(ctx: Any, universe: List[str]) -> List[Dict[str, Any]]:
    from analyst_data import recommendation_shift
    from market_data import get_bars

    out = []
    for symbol in universe:
        try:
            shift = recommendation_shift(symbol)
            if not shift or shift["direction"] == "flat":
                continue
            # Need a meaningful base of analysts to consider the shift
            # signal-worthy — small coverage = noisy.
            if shift["total_analysts"] < 5:
                continue

            df = get_bars(symbol, limit=5)
            if df is None or len(df) < 2:
                continue
            price = float(df["close"].iloc[-1])
            prior = float(df["close"].iloc[-2])
            move_pct = (price - prior) / prior * 100 if prior > 0 else 0

            # Require price to confirm the revision direction.
            is_upgrade = shift["direction"] == "bullish"
            if is_upgrade and move_pct < 0:
                continue
            if not is_upgrade and move_pct > 0:
                continue

            signal = "BUY" if is_upgrade else "SELL"
            out.append({
                "symbol": symbol,
                "signal": signal,
                "score": 1,
                "votes": {NAME: signal},
                "price": price,
                "reason": (
                    f"Analyst consensus shift {shift['direction']} "
                    f"(score {shift['oldest_score']:+.2f} → "
                    f"{shift['current_score']:+.2f}, "
                    f"count_shift {shift['count_shift']:+d}, "
                    f"{shift['total_analysts']} analysts), "
                    f"price confirming ({move_pct:+.1f}%)"
                ),
            })
        except (KeyError, ValueError, AttributeError, TypeError,
                IndexError, ZeroDivisionError, OSError) as _ss_exc:
            logger.debug(
                "%s scoring failed for %s: %s: %s",
                NAME, symbol, type(_ss_exc).__name__, _ss_exc,
            )
            continue
    return out
