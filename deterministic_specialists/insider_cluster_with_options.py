"""CONFIRM LONG when insider buying cluster + unusual bullish
options flow stack — institutional + executive consensus."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "insider_cluster_with_options"
DESCRIPTION = "CONFIRM LONG when insider cluster + bullish UOA stack"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    cluster = alt.get("insider_cluster") or {}
    opts = alt.get("options") or {}
    if not (cluster.get("is_cluster") or cluster.get("cluster_detected")):
        return None
    if cluster.get("cluster_direction") != "buying":
        return None
    if not opts.get("unusual"):
        return None
    pcr = opts.get("put_call_ratio")
    try:
        pcr_f = float(pcr) if pcr is not None else 1.0
    except (TypeError, ValueError):
        pcr_f = 1.0
    if pcr_f >= 0.7:
        return None
    return {"severity": "CONFIRM",
            "reasoning": f"Insider cluster buying + bullish unusual options (P/C {pcr_f:.2f}). Executive + institutional consensus."}
