"""Risk-budget (risk-parity) sizing.

P4.4 of LONG_SHORT_PLAN.md. Risk parity says each position should
contribute equal variance to the portfolio. Equal-DOLLAR weights are
NOT equal-RISK weights — a 5% slug of a 60%-vol biotech contributes
~3× the variance of a 5% slug of a 20%-vol utility. To equalize, you
weight INVERSELY to vol:

    w_i ∝ 1 / σ_i

This module:
  - flags positions whose risk contribution (weight × annualized vol)
    is way out of band relative to the portfolio average
  - computes a base "vol-aware size scale" suggestion for new entries
  - renders both as an AI prompt block so position sizing can shift
    without overriding the configured max_position_pct

The output is soft guidance — does NOT replace the per-trade Kelly
or drawdown scale. Layer the multipliers:

    final_size = base × kelly × drawdown_scale × vol_scale
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Reference annualized vol used as the "1.0× scale" anchor. Picks a
# typical mid-cap value so a 25%-vol stock gets ~normal sizing,
# 50%-vol gets cut in half, 12.5%-vol can stretch to 2× (capped).
TARGET_VOL = 0.25

# Bounds on the per-name vol scale so we never recommend extreme sizes
# from a single noisy vol estimate.
VOL_SCALE_MIN = 0.40
VOL_SCALE_MAX = 1.60

# A position's risk contribution is "high" when it's > this multiple
# of the average per-position contribution. Flagging threshold for the
# AI prompt — names above are candidates to trim.
HIGH_CONTRIB_MULTIPLE = 2.0
LOW_CONTRIB_MULTIPLE = 0.5


def compute_vol_scale(realized_vol: Optional[float],
                       target_vol: float = TARGET_VOL) -> float:
    """Return a base-size multiplier that targets equal portfolio variance.

    realized_vol is annualized (e.g., 0.25 = 25% annualized).
    Returns 1.0 when vol data is missing — degrade gracefully rather
    than block sizing.
    """
    if realized_vol is None or realized_vol <= 0:
        return 1.0
    raw = target_vol / realized_vol
    return max(VOL_SCALE_MIN, min(VOL_SCALE_MAX, raw))


def analyze_position_risk(
    positions: List[Dict[str, Any]],
    equity: float,
) -> Optional[Dict[str, Any]]:
    """For each position, compute weight × annualized vol and flag
    outliers vs the per-position average.

    Returns:
      {
        "contributions": [
          {"symbol": "AAPL", "weight": 0.05, "vol": 0.22,
           "contribution": 0.011, "ratio": 1.0, "tag": "normal"},
          ...
        ],
        "avg_contribution": 0.0098,
        "high_contrib_threshold": 0.0196,
        "high_contributors": ["TSLA"],
        "low_contributors": [],
      }
    Returns None when positions list is empty or equity ≤ 0.
    """
    if not positions or not equity or equity <= 0:
        return None
    try:
        from factor_data import get_realized_vol
    except Exception:
        return None

    rows: List[Dict[str, Any]] = []
    for p in positions:
        sym = p.get("symbol")
        mv = abs(float(p.get("market_value") or 0))
        if not sym or mv <= 0:
            continue
        weight = mv / equity
        vol = get_realized_vol(sym)
        if vol is None:
            # Skip — including unknown vol would distort the average.
            continue
        contribution = weight * vol
        rows.append({
            "symbol": sym,
            "weight": weight,
            "vol": vol,
            "contribution": contribution,
        })

    if len(rows) < 2:
        # Need at least two positions to compute an average meaningfully.
        return None

    avg = sum(r["contribution"] for r in rows) / len(rows)
    high_thr = avg * HIGH_CONTRIB_MULTIPLE
    low_thr = avg * LOW_CONTRIB_MULTIPLE

    high_syms: List[str] = []
    low_syms: List[str] = []
    for r in rows:
        if avg > 0:
            r["ratio"] = r["contribution"] / avg
        else:
            r["ratio"] = 1.0
        if r["contribution"] >= high_thr:
            r["tag"] = "high"
            high_syms.append(r["symbol"])
        elif r["contribution"] <= low_thr:
            r["tag"] = "low"
            low_syms.append(r["symbol"])
        else:
            r["tag"] = "normal"

    return {
        "contributions": sorted(rows, key=lambda x: -x["contribution"]),
        "avg_contribution": avg,
        "high_contrib_threshold": high_thr,
        "low_contrib_threshold": low_thr,
        "high_contributors": high_syms,
        "low_contributors": low_syms,
    }


def render_for_prompt(analysis: Optional[Dict[str, Any]]) -> str:
    """Format risk-budget analysis as an AI prompt block.

    Suppresses the block when there's nothing actionable (no positions,
    no outliers). The block always includes the sizing rule so the
    AI knows to use vol-inverse weighting on new entries.
    """
    if not analysis:
        return ""
    high = analysis.get("high_contributors") or []
    low = analysis.get("low_contributors") or []
    avg = analysis.get("avg_contribution") or 0
    if not high and not low and len(analysis.get("contributions") or []) < 3:
        # Not enough signal to bother the AI.
        return ""
    lines = [
        "\nRISK-BUDGET (variance contribution by name):",
        f"  Avg per-name risk contribution: {avg*100:.2f}% "
        f"(weight × annualized vol).",
        f"  Sizing rule: scale base size by ({TARGET_VOL:.0%} / annualized_vol), "
        f"clamp [{VOL_SCALE_MIN:.1f}×, {VOL_SCALE_MAX:.1f}×]. "
        "High-vol names = smaller bets.",
    ]
    if high:
        items = [
            f"{r['symbol']} ({r['ratio']:.1f}× avg, vol {r['vol']*100:.0f}%)"
            for r in analysis["contributions"] if r.get("tag") == "high"
        ][:5]
        lines.append(
            f"  → OVER-CONTRIBUTING (>{HIGH_CONTRIB_MULTIPLE:.0f}× avg risk): "
            + ", ".join(items)
            + ". Trim or avoid stacking similar-risk picks."
        )
    if low:
        items = [
            f"{r['symbol']} ({r['ratio']:.1f}× avg, vol {r['vol']*100:.0f}%)"
            for r in analysis["contributions"] if r.get("tag") == "low"
        ][:5]
        lines.append(
            f"  → UNDER-CONTRIBUTING (<{LOW_CONTRIB_MULTIPLE:.1f}× avg risk): "
            + ", ".join(items)
            + ". Capacity for additional sizing here if conviction supports."
        )
    return "\n".join(lines) + "\n"
