"""Capacity-aware scaling projection for the Performance > Scalability tab.

Each row shows what the strategy would look like at that capital level
*if you migrated to the appropriate profile type for that scale*.

A user with $10K running a Small Cap profile shouldn't see "what would
happen if I dumped $10M into Small Caps" — nobody would do that. The
sensible projection is: at $5M+ you'd be on a Large Cap profile, so
the row uses Large Cap's typical liquidity profile.

## Two effects compound at each rung

1. **Square-root market impact** within a tier:
       slippage ∝ √(position_size / daily_$volume)
   Doubling capital → ~1.4× slippage. 10× capital → ~3.16× slippage.
2. **Tier migration** improves liquidity:
       Mid-cap names trade ~10× more $ volume than small-caps.
       Large-cap names trade ~10× more $ volume than mid-caps.
   At the same position size, slippage on mid-caps is ~√10 ≈ 0.32× small-caps.

Combined formula (relative to the user's *currently observed* slippage):

    slippage(C) = base_slip × √(C / current_capital × current_$ADV / target_tier_$ADV)

When the migration ladder is followed, slippage stays roughly flat
across scales — that's the WHOLE POINT of the migration ladder. If
the user does NOT migrate, slippage explodes (sqrt of capital growth
without the liquidity offset). Both stories matter.

## Calibration baseline

Slippage starts from the user's *observed* fill data on whatever
profile they're currently running. The migration math then translates
that observed value into projections for other tiers using empirical
$ADV ratios.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional


# Empirical typical $ADV by tier (rough averages across each cap tier).
_ADV_BY_TIER = {
    "micro":  500_000,
    "small":  5_000_000,
    "mid":    50_000_000,
    "large":  500_000_000,
    "crypto": 100_000_000,
}

# Human labels for tier names (UI-friendly).
_TIER_LABEL = {
    "micro":  "Micro Cap",
    "small":  "Small Cap",
    "mid":    "Mid Cap",
    "large":  "Large Cap",
    "crypto": "Crypto",
}

# Migration ladder: at each capital threshold, the recommended profile
# type changes. Lower thresholds = smaller-cap profiles.
# Note: a Small Cap profile at $250K is at its soft max; above that the
# *recommended* path is to migrate the capital to a Mid Cap profile.
_MIGRATION_LADDER = [
    # (max_capital, recommended_tier)
    (250_000,        "small"),
    (5_000_000,      "mid"),
    (50_000_000,     "large"),
    (float("inf"),   "large"),  # cap at large
]

# For micro-only systems, the migration is similar but starts smaller.
_MIGRATION_LADDER_MICRO_BASE = [
    (50_000,         "micro"),
    (250_000,        "small"),
    (5_000_000,      "mid"),
    (50_000_000,     "large"),
    (float("inf"),   "large"),
]

# Default capital ladder shown on the Scalability tab.
DEFAULT_LADDER = [
    (10_000,    "$10K (current)"),
    (50_000,    "$50K"),
    (100_000,   "$100K"),
    (500_000,   "$500K"),
    (1_000_000, "$1M"),
    (10_000_000,"$10M"),
]


def _normalize_market_type(market_type: str) -> str:
    """Collapse the various market_type spellings to a canonical tier."""
    mt = (market_type or "").lower()
    if mt in ("micro", "microcap"):
        return "micro"
    if mt in ("small", "smallcap"):
        return "small"
    if mt in ("mid", "midcap"):
        return "mid"
    if mt in ("large", "largecap"):
        return "large"
    if mt == "crypto":
        return "crypto"
    return "small"


def _recommended_tier(capital: float, market_type: str) -> str:
    """Return the recommended profile tier for a given capital level.
    Crypto stays crypto at all capital levels (separate ladder)."""
    if market_type == "crypto":
        return "crypto"
    ladder = _MIGRATION_LADDER_MICRO_BASE if market_type == "micro" else _MIGRATION_LADDER
    for max_cap, tier in ladder:
        if capital <= max_cap:
            return tier
    return "large"


def _ci_factor(n_trades: int) -> float:
    """Confidence-interval half-width as a multiplier on the point estimate."""
    if n_trades < 10:
        return 1.00
    if n_trades < 30:
        return 0.50
    if n_trades < 100:
        return 0.25
    return 0.10


# Slippage multiplier when limit orders are used vs market orders.
# Empirical: limit orders cut realized slippage by ~50-70% on liquid
# US equities (some adverse-selection cost remains). 0.40× = 60%
# reduction, conservative middle of the range.
_LIMIT_ORDER_SLIPPAGE_MULT = 0.40


def project_scaling(
    trades: List[Dict[str, Any]],
    current_capital: float,
    base_net_return_pct: float,
    market_type: str = "small",
    use_limit_orders_now: bool = False,
    ladder: Optional[List] = None,
) -> Dict[str, Any]:
    """Project slippage and return at each capital level for BOTH
    execution styles (market vs limit), so the user can compare and
    make an informed choice. The recommended profile tier for each
    capital level is also shown.

    Args:
        trades: list of trade dicts (must include `slippage_pct` for
                trades that have fill data)
        current_capital: starting capital (drives scale multipliers)
        base_net_return_pct: strategy's currently-observed net return
        market_type: profile market type (drives migration ladder)
        use_limit_orders_now: whether the current profile already uses
            limit orders. If True, the observed baseline reflects limit
            execution; we back it out to estimate the market-equivalent.
        ladder: optional override of (capital, label) pairs
    """
    if ladder is None:
        ladder = DEFAULT_LADDER

    fill_slips = [abs(t.get("slippage_pct", 0) or 0) for t in trades
                  if t.get("slippage_pct") is not None
                  and t.get("slippage_pct") != 0]
    n_with_fills = len(fill_slips)
    canonical_mt = _normalize_market_type(market_type)
    current_adv = _ADV_BY_TIER.get(canonical_mt, _ADV_BY_TIER["small"])

    if n_with_fills == 0:
        return {
            "rows": [],
            "data_quality": "insufficient",
            "n_trades_with_fills": 0,
            "base_slippage_pct": 0.0,
            "market_type": canonical_mt,
            "current_tier_label": _TIER_LABEL.get(canonical_mt, canonical_mt),
            "message": (
                "Slippage projections need trades with both the price we expected and "
                "the price we actually got. Run more trades to populate this section."
            ),
        }

    base_slip_pct = sum(fill_slips) / n_with_fills

    # Translate the observed baseline into both execution styles.
    # If the user is currently on limits, baseline reflects that — back
    # it out to estimate market-equivalent. If they're on market orders,
    # multiply by 0.4 to estimate what limits would yield.
    if use_limit_orders_now:
        base_slip_limit = base_slip_pct
        base_slip_market = base_slip_pct / _LIMIT_ORDER_SLIPPAGE_MULT
    else:
        base_slip_market = base_slip_pct
        base_slip_limit = base_slip_pct * _LIMIT_ORDER_SLIPPAGE_MULT

    ci_mult = _ci_factor(n_with_fills)

    rows = []
    for capital, label in ladder:
        target_tier = _recommended_tier(capital, canonical_mt)
        target_adv = _ADV_BY_TIER.get(target_tier, current_adv)
        migrated = (target_tier != canonical_mt)

        # Square-root market impact × tier-liquidity adjustment.
        if current_capital <= 0 or current_adv <= 0:
            scale_root = 1.0
        else:
            scale_factor = max((capital / current_capital) * (current_adv / target_adv), 0.0)
            scale_root = math.sqrt(scale_factor)

        slip_market = base_slip_market * scale_root
        slip_limit = base_slip_limit * scale_root

        # Slippage growth eats into return 1:1 (it's already a per-trade %).
        # No clipping — improvements (slippage going below current baseline)
        # boost return.
        return_market = base_net_return_pct - (slip_market - base_slip_pct)
        return_limit = base_net_return_pct - (slip_limit - base_slip_pct)

        notes = []
        if migrated:
            notes.append(
                f"At this scale, you'd switch to a {_TIER_LABEL[target_tier]} "
                f"profile. The bigger universe gives you ~"
                f"{int(target_adv / current_adv)}× more daily volume per name, "
                f"which keeps slippage manageable as positions grow."
            )

        rows.append({
            "label": label,
            "capital": capital,
            "recommended_tier": target_tier,
            "recommended_tier_label": _TIER_LABEL.get(target_tier, target_tier),
            "migrated": migrated,
            "slippage_market_pct": round(slip_market, 4),
            "slippage_market_pct_low": round(slip_market * (1 - ci_mult), 4),
            "slippage_market_pct_high": round(slip_market * (1 + ci_mult), 4),
            "slippage_limit_pct": round(slip_limit, 4),
            "slippage_limit_pct_low": round(slip_limit * (1 - ci_mult), 4),
            "slippage_limit_pct_high": round(slip_limit * (1 + ci_mult), 4),
            "return_market_pct": round(return_market, 2),
            "return_limit_pct": round(return_limit, 2),
            "notes": notes,
        })

    quality = "calibrated" if n_with_fills >= 30 else "modeled"
    return {
        "rows": rows,
        "data_quality": quality,
        "n_trades_with_fills": n_with_fills,
        "base_slippage_pct": round(base_slip_pct, 4),
        "market_type": canonical_mt,
        "current_tier_label": _TIER_LABEL.get(canonical_mt, canonical_mt),
        "use_limit_orders_now": use_limit_orders_now,
        "limit_order_reduction_pct": round((1 - _LIMIT_ORDER_SLIPPAGE_MULT) * 100),
    }
