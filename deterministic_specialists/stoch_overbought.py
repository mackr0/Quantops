"""CAUTION LONG when Stochastic RSI > 80 (short-term overbought)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "stoch_overbought"
DESCRIPTION = "CAUTION LONG when Stochastic RSI > 80"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    s = candidate.get("stoch_rsi")
    if s is None:
        return None
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    if v >= 80:
        return {"severity": "CAUTION",
                "reasoning": f"StochRSI {v:.0f} ≥ 80 — short-term overbought; entry late in the swing."}
    return None
