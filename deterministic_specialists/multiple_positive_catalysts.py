"""CONFIRM LONG when 2+ positive catalysts stack (insider cluster,
13D, dark-pool accumulation, EPS revisions up, congressional buying,
positive transcript, patent acceleration, star-manager holding)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "multiple_positive_catalysts"
DESCRIPTION = "CONFIRM LONG when 2+ positive catalysts stack"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    flags = []
    if (alt.get("insider_cluster") or {}).get("is_cluster") or \
       (alt.get("insider_cluster") or {}).get("cluster_detected"):
        if (alt.get("insider_cluster") or {}).get("cluster_direction") == "buying":
            flags.append("insider_cluster")
    if (alt.get("activist_13dg") or {}).get("has_13d"):
        flags.append("13D")
    dp = alt.get("dark_pool") or {}
    if (dp.get("ats_volume", 0) or 0) >= 100_000:
        flags.append("darkPool")
    if (alt.get("analyst_estimates") or {}).get("eps_revision_direction", "").lower() in ("up", "higher", "bullish"):
        flags.append("EPS+")
    cong = alt.get("congressional_recent") or {}
    if cong.get("net_direction") == "buying":
        flags.append("congress+")
    t = alt.get("transcript_sentiment") or {}
    if t.get("has_data") and (t.get("tone") or "").lower() in ("bullish", "positive", "confident"):
        flags.append("transcript+")
    if (alt.get("star_manager_holdings") or {}).get("holders"):
        flags.append("starManager")
    if len(flags) < 2:
        return None
    return {"severity": "CONFIRM",
            "reasoning": f"Multiple positive catalysts stack: {' + '.join(flags)}. Compound smart-money + flow alignment."}
