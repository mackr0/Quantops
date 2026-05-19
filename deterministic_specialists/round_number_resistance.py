"""CAUTION LONG when price is approaching a round-number resistance
($50, $100, $200, $500, $1000). Round numbers attract limit sells
from retail and stops from prior buyers — momentum often stalls
there.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "round_number_resistance"
DESCRIPTION = "CAUTION LONG when price within 1% of a round-number level"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")

_ROUND_LEVELS = (50, 100, 150, 200, 250, 300, 500, 750, 1000)
_PROXIMITY_PCT = 1.0


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    p = candidate.get("price")
    if p is None:
        return None
    try:
        price = float(p)
    except (TypeError, ValueError):
        return None
    for level in _ROUND_LEVELS:
        if level * 0.99 <= price < level:
            return {"severity": "CAUTION",
                    "reasoning": f"Price ${price:.2f} approaching round-number resistance at ${level}. Retail limit sells + prior-buyer stops cluster here."}
    return None
