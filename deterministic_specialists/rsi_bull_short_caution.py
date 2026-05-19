"""CAUTION SHORT when RSI > 50 (shorting into bullish backdrop)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "rsi_bull_short_caution"
DESCRIPTION = "CAUTION SHORT when RSI > 55 (shorting into bullish backdrop)"
APPLIES_TO_SIGNALS = ("SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    rsi = candidate.get("rsi")
    if rsi is None:
        return None
    try:
        v = float(rsi)
    except (TypeError, ValueError):
        return None
    if v >= 55:
        return {"severity": "CAUTION",
                "reasoning": f"RSI {v:.0f} above midline — shorting into a bullish backdrop. Edge requires specific catalyst."}
    return None
