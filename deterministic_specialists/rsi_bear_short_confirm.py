"""CONFIRM SHORT when RSI < 45 (shorting with bearish backdrop)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "rsi_bear_short_confirm"
DESCRIPTION = "CONFIRM SHORT when RSI < 45 (bearish backdrop)"
APPLIES_TO_SIGNALS = ("SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    rsi = candidate.get("rsi")
    if rsi is None:
        return None
    try:
        v = float(rsi)
    except (TypeError, ValueError):
        return None
    if 25 <= v < 45:  # not yet capitulation-oversold
        return {"severity": "CONFIRM",
                "reasoning": f"RSI {v:.0f} below midline — SHORT with the bearish backdrop."}
    return None
