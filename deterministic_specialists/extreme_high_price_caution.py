"""CAUTION on entries for very-high-priced names (>$1000 per share)
— sizing becomes blocky, fewer shares per dollar, partial fills
common."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "extreme_high_price_caution"
DESCRIPTION = "CAUTION when price > $1000 (sizing/fill quality concerns)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    p = candidate.get("price")
    if p is None:
        return None
    try:
        price = float(p)
    except (TypeError, ValueError):
        return None
    if price > 1000:
        return {"severity": "CAUTION",
                "reasoning": f"Price ${price:.2f} — sizing becomes blocky (fewer shares per $); partial fills + slippage on entries/exits."}
    return None
