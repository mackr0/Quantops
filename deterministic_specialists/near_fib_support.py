"""CONFIRM LONG when price is at a Fibonacci support level.

Fib levels (38.2%, 50%, 61.8%) tend to be self-fulfilling because
enough traders watch them. Within 1% of a Fib level on a pullback
is a higher-probability entry than the same RSI/oversold reading
mid-range.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "near_fib_support"
DESCRIPTION = "CONFIRM LONG when within 1% of a Fibonacci support level"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    fib = candidate.get("nearest_fib_dist")
    if fib is None:
        return None
    try:
        f = float(fib)
    except (TypeError, ValueError):
        return None
    if f >= 1.0:
        return None
    return {
        "severity": "CONFIRM",
        "reasoning": (
            f"Within {f:.1f}% of a Fibonacci level. Self-fulfilling "
            "supports — higher-probability entry than mid-range pullbacks."
        ),
    }
