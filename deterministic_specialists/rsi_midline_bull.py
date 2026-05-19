"""CONFIRM LONG when RSI > 50 (above midline = bullish backdrop).

Wilder's RSI midline cross is the simplest trend filter. RSI
sustainably above 50 means upmoves dominate. Combined with a BUY
signal this is a baseline trend confirmation.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "rsi_midline_bull"
DESCRIPTION = "CONFIRM LONG when RSI > 50 (above midline = bullish backdrop)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    rsi = candidate.get("rsi")
    if rsi is None:
        return None
    try:
        v = float(rsi)
    except (TypeError, ValueError):
        return None
    # Strict above midline but not yet overbought (avoid double-firing
    # with rsi_overbought_late_stage / parabolic_blow_off)
    if 55 <= v <= 70:
        return {"severity": "CONFIRM",
                "reasoning": f"RSI {v:.0f} above 55 — trend backdrop favors longs."}
    return None
