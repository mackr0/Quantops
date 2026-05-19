"""CONFIRM LONG when price just bounced off a round-number support
(within 1% above $50/$100/etc)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "round_number_support"
DESCRIPTION = "CONFIRM LONG when price within 1% above a round-number level"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")

_ROUND_LEVELS = (50, 100, 150, 200, 250, 300, 500, 750, 1000)


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    p = candidate.get("price")
    if p is None:
        return None
    try:
        price = float(p)
    except (TypeError, ValueError):
        return None
    for level in _ROUND_LEVELS:
        if level < price <= level * 1.01:
            return {"severity": "CONFIRM",
                    "reasoning": f"Price ${price:.2f} just above ${level} round-number support. Retail limit buys + prior-shorter stops cluster here."}
    return None
