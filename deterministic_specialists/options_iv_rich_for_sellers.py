"""CONFIRM short-premium options strategies when IV rank > 60."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "options_iv_rich_for_sellers"
DESCRIPTION = "CONFIRM option-sell strategy when IV rank > 60"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    opts = alt.get("options") or {}
    iv = opts.get("iv_rank")
    if iv is None:
        return None
    try:
        v = float(iv)
    except (TypeError, ValueError):
        return None
    if 60 <= v < 75:  # Below "extreme high" tier
        return {"severity": "CONFIRM",
                "reasoning": f"IV rank {v:.0f} — premium-rich for option sellers; covered call / cash-secured put have edge."}
    return None
