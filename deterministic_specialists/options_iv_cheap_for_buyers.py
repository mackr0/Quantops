"""CONFIRM directional-options buyer when IV rank < 25."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "options_iv_cheap_for_buyers"
DESCRIPTION = "CONFIRM option-buy strategy when IV rank < 25"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


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
    if v < 25:
        return {"severity": "CONFIRM",
                "reasoning": f"IV rank {v:.0f} — premium cheap; long-option directional bets get the edge from cheap optionality."}
    return None
