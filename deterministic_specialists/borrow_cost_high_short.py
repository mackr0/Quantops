"""CAUTION SHORT when borrow cost is high.

HTB names with 5-50%+ annual borrow rate eat real return on
multi-day shorts. A short that needs 3 weeks to play out can lose
2-3% to financing alone — destroys the edge for anything but a
true conviction multi-day short.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "borrow_cost_high_short"
DESCRIPTION = "CAUTION SHORT when borrow is HTB / high-cost"
APPLIES_TO_SIGNALS = ("SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    cost = candidate.get("_borrow_cost")
    rate_str = candidate.get("_borrow_rate_str")
    if cost != "high" and not rate_str:
        return None
    detail = rate_str or f"{cost} cost"
    return {
        "severity": "CAUTION",
        "reasoning": (
            f"Short borrow {detail}. Multi-day holds will eat "
            "2-3%+ in financing alone — confirm the edge survives."
        ),
    }
