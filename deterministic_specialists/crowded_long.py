"""CAUTION LONG when the trade is consensus / crowded.

Two cheap proxies for "everyone is already long":
  - Short volume ratio is unusually LOW (very few traders shorting
    this on a daily basis = no one is positioning against it)
  - Analyst-estimates direction is consensus-bullish

When BOTH agree, the upside is largely priced in — entry near
this state has historically been a low-edge entry because the
marginal buyer has already bought.

CAUTION rather than VETO — consensus longs can keep working if the
trend is strong. The point is to surface that there's no edge from
contrarian positioning.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


NAME = "crowded_long"
DESCRIPTION = "CAUTION LONG when short_vol_ratio < 0.15 AND analyst consensus is bullish"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")

_SHORT_VOL_RATIO_THRESHOLD = 0.15


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    finra = alt.get("finra_short_vol") or {}
    estimates = alt.get("analyst_estimates") or {}
    svr = finra.get("short_volume_ratio")
    if svr is None:
        return None
    try:
        svr_f = float(svr)
    except (TypeError, ValueError):
        return None
    if svr_f >= _SHORT_VOL_RATIO_THRESHOLD:
        return None  # not crowded by this proxy
    # Bullish analyst consensus is the second leg
    eps_dir = estimates.get("eps_revision_direction") or ""
    consensus_bullish = eps_dir.lower() in ("up", "higher", "bullish")
    if not consensus_bullish:
        return None
    return {
        "severity": "CAUTION",
        "reasoning": (
            f"Short-volume ratio {svr_f:.0%} (very low — few shorts) "
            f"AND analyst EPS revisions {eps_dir.upper()}. Crowded long "
            "— marginal buyer has likely already bought."
        ),
    }
