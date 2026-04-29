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
from typing import Any, Dict, List, Optional, Tuple


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

# Default capital ladder shown on the Scalability tab when nothing
# more specific is requested — only used as a fallback. Real callers
# should hand in their actual capital so build_ladder() can generate
# tiers centered on it (the "(current)" label is dynamic).
DEFAULT_LADDER = [
    (10_000,    "$10K"),
    (50_000,    "$50K"),
    (100_000,   "$100K"),
    (500_000,   "$500K"),
    (1_000_000, "$1M"),
    (10_000_000,"$10M"),
]


def _format_capital(amount: float) -> str:
    """Render a capital amount as a short label like '$2.15M' or '$25K'."""
    if amount >= 1_000_000_000:
        return f"${amount / 1_000_000_000:.1f}B"
    if amount >= 1_000_000:
        # 2.15M, 5M, 25M etc. — drop the .0 on round numbers.
        m = amount / 1_000_000
        return f"${m:.1f}M" if m % 1 else f"${m:.0f}M"
    if amount >= 1_000:
        k = amount / 1_000
        return f"${k:.0f}K"
    return f"${amount:.0f}"


def build_ladder(current_capital: float) -> List[Tuple[int, str]]:
    """Generate a capital ladder anchored on the user's actual capital.

    Includes ~2 rows below current and ~3 above so the user sees both
    'where I am' and 'how does this scale.' The current row carries
    the '(current)' label so the table never lies about which level
    they're actually running at.
    """
    if current_capital <= 0:
        return DEFAULT_LADDER

    # Round current to a nice number for the label, but use the actual
    # value internally so the projection math is precise.
    cur = float(current_capital)
    cur_label = _format_capital(cur) + " (current)"

    # Build a logarithmic spread: 0.1×, 0.25×, current, 2×, 5×, 25×.
    # Caps:
    #   - lower bound at $10K so we never project below tradeable
    #   - upper bound at $100M so the high-end stays interpretable.
    candidates = [
        (max(int(cur * 0.1), 10_000), None),
        (max(int(cur * 0.25), 10_000), None),
        (int(cur), cur_label),
        (min(int(cur * 2), 100_000_000), None),
        (min(int(cur * 5), 100_000_000), None),
        (min(int(cur * 25), 100_000_000), None),
    ]

    # Drop duplicates (e.g. when cur is small enough that 0.1×=0.25×=$10K)
    # and label rows that aren't already labeled.
    seen = set()
    out = []
    for cap, label in candidates:
        if cap in seen:
            continue
        seen.add(cap)
        out.append((cap, label or _format_capital(cap)))
    return out


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


# Default theoretical scaling tiers — round numbers, not formula-derived.
# Always shown ABOVE the user's current capital.
DEFAULT_THEORETICAL_LADDER = [
    (5_000_000,    "$5M"),
    (10_000_000,   "$10M"),
    (25_000_000,   "$25M"),
    (50_000_000,   "$50M"),
    (100_000_000,  "$100M"),
]


def per_profile_breakdown(profile_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build one row per actual profile with REAL measured slippage / return.

    profile_data: list of dicts, one per profile, with these keys:
        - name (str)
        - capital (float, initial capital)
        - market_type (str)
        - trades (list of trade dicts from that profile's journal)
        - latest_equity (float, most recent snapshot equity)

    Returns one row per profile with measured-only fields. No projection
    math. Profiles with no fills yet show measured_slippage_pct=None.
    """
    rows = []
    for p in profile_data:
        trades = p.get("trades", []) or []
        slips = [abs(t.get("slippage_pct") or 0) for t in trades
                 if t.get("slippage_pct") is not None
                 and (t.get("slippage_pct") or 0) != 0]
        sizes = [(t.get("price") or 0) * (t.get("qty") or 0) for t in trades
                 if (t.get("price") or 0) > 0 and (t.get("qty") or 0) > 0]
        avg_slip = (sum(slips) / len(slips)) if slips else None
        avg_size = (sum(sizes) / len(sizes)) if sizes else 0.0
        cap = p.get("capital") or 0
        last_eq = p.get("latest_equity") or cap
        ret_pct = ((last_eq - cap) / cap * 100) if cap > 0 else 0.0
        rows.append({
            "name": p.get("name", ""),
            "capital": cap,
            "tier_label": _TIER_LABEL.get(
                _normalize_market_type(p.get("market_type") or "small"),
                p.get("market_type") or "—"
            ),
            "avg_position_size": round(avg_size, 2),
            "measured_slippage_pct": round(avg_slip, 4) if avg_slip is not None else None,
            "measured_return_pct": round(ret_pct, 2),
            "n_trades": len(trades),
            "n_trades_with_fills": len(slips),
        })
    return rows


def theoretical_scaling(
    baseline_slip_pct: float,
    baseline_capital: float,
    baseline_market_type: str,
    base_return_pct: float,
    n_trades_with_fills: int,
    use_limit_orders_now: bool = False,
    ladder: Optional[List[Tuple[int, str]]] = None,
) -> Dict[str, Any]:
    """Project slippage at hypothetical capital tiers above current.

    Only includes ladder rows STRICTLY above baseline_capital — there's
    no point projecting downward from your real position. Each row carries
    a `tier_label` (the universe of names that capital level would
    typically run in: Mid / Large) but no 'migrated' flag, since that
    word doesn't apply to a hypothetical scale-up.
    """
    if ladder is None:
        ladder = DEFAULT_THEORETICAL_LADDER

    if n_trades_with_fills == 0 or baseline_slip_pct <= 0 or baseline_capital <= 0:
        return {
            "rows": [],
            "data_quality": "insufficient",
            "message": (
                "Slippage projections need trades with both the price we expected and "
                "the price we actually got. Run more trades to populate this section."
            ),
            "baseline_capital": baseline_capital,
            "baseline_slippage_pct": round(baseline_slip_pct, 4),
            "limit_order_reduction_pct": round((1 - _LIMIT_ORDER_SLIPPAGE_MULT) * 100),
        }

    # Translate the observed baseline into both execution styles. If
    # the user is currently on limits, baseline reflects that — back
    # it out to estimate market-equivalent. Else multiply.
    if use_limit_orders_now:
        base_slip_limit = baseline_slip_pct
        base_slip_market = baseline_slip_pct / _LIMIT_ORDER_SLIPPAGE_MULT
    else:
        base_slip_market = baseline_slip_pct
        base_slip_limit = baseline_slip_pct * _LIMIT_ORDER_SLIPPAGE_MULT

    canonical_mt = _normalize_market_type(baseline_market_type)
    current_adv = _ADV_BY_TIER.get(canonical_mt, _ADV_BY_TIER["small"])
    ci_mult = _ci_factor(n_trades_with_fills)

    rows = []
    for cap, label in ladder:
        if cap <= baseline_capital:
            continue
        target_tier = _recommended_tier(cap, canonical_mt)
        target_adv = _ADV_BY_TIER.get(target_tier, current_adv)
        scale_factor = max((cap / baseline_capital) * (current_adv / target_adv), 0.0)
        scale_root = math.sqrt(scale_factor)
        slip_market = base_slip_market * scale_root
        slip_limit = base_slip_limit * scale_root
        return_market = base_return_pct - (slip_market - baseline_slip_pct)
        return_limit = base_return_pct - (slip_limit - baseline_slip_pct)
        rows.append({
            "capital": cap,
            "label": label,
            "tier_label": _TIER_LABEL.get(target_tier, target_tier),
            "slippage_market_pct": round(slip_market, 4),
            "slippage_market_pct_low": round(slip_market * (1 - ci_mult), 4),
            "slippage_market_pct_high": round(slip_market * (1 + ci_mult), 4),
            "slippage_limit_pct": round(slip_limit, 4),
            "slippage_limit_pct_low": round(slip_limit * (1 - ci_mult), 4),
            "slippage_limit_pct_high": round(slip_limit * (1 + ci_mult), 4),
            "return_market_pct": round(return_market, 2),
            "return_limit_pct": round(return_limit, 2),
        })

    return {
        "rows": rows,
        "data_quality": "calibrated" if n_trades_with_fills >= 30 else "modeled",
        "n_trades_with_fills": n_trades_with_fills,
        "baseline_capital": baseline_capital,
        "baseline_slippage_pct": round(baseline_slip_pct, 4),
        "baseline_tier_label": _TIER_LABEL.get(canonical_mt, canonical_mt),
        "use_limit_orders_now": use_limit_orders_now,
        "limit_order_reduction_pct": round((1 - _LIMIT_ORDER_SLIPPAGE_MULT) * 100),
    }


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
        ladder = build_ladder(current_capital)

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
            # Upgrade or downgrade? Compare ADV.
            if target_adv > current_adv and current_adv > 0:
                ratio = target_adv / current_adv
                notes.append(
                    f"At this scale, you'd switch up to a "
                    f"{_TIER_LABEL[target_tier]} profile — the larger universe "
                    f"has ~{ratio:.1f}× more daily volume per name, which keeps "
                    f"slippage manageable as positions grow."
                )
            elif target_adv < current_adv and target_adv > 0:
                ratio = current_adv / target_adv
                notes.append(
                    f"At this scale, you'd run a {_TIER_LABEL[target_tier]} "
                    f"profile instead — at smaller capital you don't need "
                    f"the larger-cap universe (which has ~{ratio:.1f}× more "
                    f"daily volume per name than you'd need)."
                )
            else:
                notes.append(
                    f"At this scale, you'd switch to a "
                    f"{_TIER_LABEL[target_tier]} profile."
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
