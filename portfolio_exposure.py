"""Portfolio exposure analysis — long/short by sector.

P2.1 of LONG_SHORT_PLAN.md. Today the dashboard knows total long
and short notional but not how it's distributed across sectors.
A profile that's "10% net long" might still be 50% long Tech and
40% short Tech (net 10%, gross 90%, sector-concentrated single
factor bet — exactly what real long/short funds try to AVOID).

This module computes:

  - aggregate net / gross / position count (existing behavior, moved here)
  - per-sector breakdown: {sector: {long_pct, short_pct, net_pct, gross_pct, n}}
  - largest sector concentration flag (warns when any sector > 30% gross)
  - directional balance (% long vs % short by sector)

The output is JSON-serializable and rendered on the Performance
Dashboard's Current Exposure section + passed to the AI prompt
so the AI can avoid stacking concentration that's already there.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


# Threshold for flagging a sector as "concentrated" — % of total
# gross exposure. Real long/short funds typically target <20% per
# sector; we use 30% as a warn threshold (not a hard cap).
SECTOR_CONCENTRATION_WARN_PCT = 30.0


def compute_exposure(
    positions: List[Dict[str, Any]],
    equity: float,
    sector_lookup=None,
) -> Dict[str, Any]:
    """Build the full exposure breakdown from a list of open positions.

    Args:
      positions: list of dicts each with keys 'symbol', 'qty', 'market_value'.
                 Long positions have qty > 0, shorts have qty < 0.
      equity: total account equity (denominator for percentages).
      sector_lookup: optional callable(symbol) -> sector_name; defaults
                     to sector_classifier.get_sector. Pass a stub for tests.

    Returns dict with:
      net_pct, gross_pct, num_positions
      by_sector: {sector_name: {long_pct, short_pct, net_pct,
                                gross_pct, n_long, n_short}}
      concentration_flags: list of sectors exceeding the warn threshold

    All numeric outputs rounded to 1 decimal. Returns empty/zero
    fields when equity <= 0 or positions is empty (caller can render
    "no exposure" state).
    """
    if equity is None or equity <= 0 or not positions:
        return {
            "net_pct": 0.0,
            "gross_pct": 0.0,
            "num_positions": 0,
            "by_sector": {},
            "concentration_flags": [],
        }

    if sector_lookup is None:
        from sector_classifier import get_sector
        sector_lookup = get_sector

    long_val = 0.0
    short_val = 0.0
    by_sector: Dict[str, Dict[str, Any]] = {}
    n_positions = 0

    for p in positions:
        sym = p.get("symbol")
        qty = float(p.get("qty", 0) or 0)
        mv = float(p.get("market_value", 0) or 0)
        if not sym or qty == 0:
            continue
        n_positions += 1

        # market_value sign: positives for longs, negatives for shorts
        # in some Alpaca shapes. Normalize: abs() and use qty for direction.
        abs_mv = abs(mv)
        is_long = qty > 0

        if is_long:
            long_val += abs_mv
        else:
            short_val += abs_mv

        try:
            sector = sector_lookup(sym) or "Unknown"
        except Exception:
            sector = "Unknown"

        bucket = by_sector.setdefault(sector, {
            "long_val": 0.0, "short_val": 0.0,
            "n_long": 0, "n_short": 0,
        })
        if is_long:
            bucket["long_val"] += abs_mv
            bucket["n_long"] += 1
        else:
            bucket["short_val"] += abs_mv
            bucket["n_short"] += 1

    # Convert sector buckets to percentages
    sector_breakdown: Dict[str, Dict[str, Any]] = {}
    concentration_flags: List[str] = []
    for sector, bucket in by_sector.items():
        long_pct = round(bucket["long_val"] / equity * 100, 1)
        short_pct = round(bucket["short_val"] / equity * 100, 1)
        net_pct = round((bucket["long_val"] - bucket["short_val"]) / equity * 100, 1)
        gross_pct = round((bucket["long_val"] + bucket["short_val"]) / equity * 100, 1)
        sector_breakdown[sector] = {
            "long_pct": long_pct,
            "short_pct": short_pct,
            "net_pct": net_pct,
            "gross_pct": gross_pct,
            "n_long": bucket["n_long"],
            "n_short": bucket["n_short"],
        }
        if gross_pct >= SECTOR_CONCENTRATION_WARN_PCT:
            concentration_flags.append(sector)

    return {
        "net_pct": round((long_val - short_val) / equity * 100, 1),
        "gross_pct": round((long_val + short_val) / equity * 100, 1),
        "num_positions": n_positions,
        "by_sector": sector_breakdown,
        "concentration_flags": concentration_flags,
    }


def render_for_prompt(exposure: Dict[str, Any]) -> str:
    """Format the exposure breakdown as a compact string for the AI prompt.

    Returns at most ~6 lines of text. Used by ai_analyst._build_batch_prompt
    to give the AI portfolio-aware context: "you're already 35% long Tech,
    don't stack another Tech long unless conviction is unusually high."
    """
    if not exposure or exposure.get("num_positions", 0) == 0:
        return "  No open positions."

    lines = [
        f"  Net: {exposure['net_pct']:+.1f}% | "
        f"Gross: {exposure['gross_pct']:.1f}% | "
        f"{exposure['num_positions']} positions"
    ]

    by_sector = exposure.get("by_sector") or {}
    if not by_sector:
        return "\n".join(lines)

    # Sort sectors by gross exposure desc; show top 5 + a tail count
    items = sorted(by_sector.items(),
                    key=lambda kv: kv[1]["gross_pct"], reverse=True)
    top = items[:5]
    rest = items[5:]

    lines.append("  By sector (top 5 by gross):")
    for sector, b in top:
        flag = "  ⚠ CONCENTRATED" if sector in exposure.get("concentration_flags", []) else ""
        if b["n_long"] and b["n_short"]:
            lines.append(
                f"    {sector}: long {b['long_pct']:.1f}% "
                f"({b['n_long']}) / short {b['short_pct']:.1f}% "
                f"({b['n_short']}) = net {b['net_pct']:+.1f}%{flag}"
            )
        elif b["n_long"]:
            lines.append(
                f"    {sector}: long {b['long_pct']:.1f}% "
                f"({b['n_long']} pos){flag}"
            )
        else:
            lines.append(
                f"    {sector}: short {b['short_pct']:.1f}% "
                f"({b['n_short']} pos){flag}"
            )
    if rest:
        rest_gross = sum(b["gross_pct"] for _, b in rest)
        lines.append(f"    + {len(rest)} other sectors ({rest_gross:.1f}% gross)")

    if exposure.get("concentration_flags"):
        lines.append(
            f"  CONCENTRATION WARNING: {', '.join(exposure['concentration_flags'])} "
            f">= {int(SECTOR_CONCENTRATION_WARN_PCT)}% of book — avoid stacking."
        )
    return "\n".join(lines)
