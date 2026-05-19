"""CONFIRM LONG when RSI, StochRSI, AND MFI all in oversold zone."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "triple_oversold"
DESCRIPTION = "CONFIRM LONG when RSI ≤ 30 + StochRSI ≤ 20 + MFI ≤ 20"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    rsi = candidate.get("rsi")
    stoch = candidate.get("stoch_rsi")
    mfi = candidate.get("mfi")
    if rsi is None or stoch is None or mfi is None:
        return None
    try:
        r = float(rsi); s = float(stoch); m = float(mfi)
    except (TypeError, ValueError):
        return None
    if r <= 30 and s <= 20 and m <= 20:
        return {"severity": "CONFIRM",
                "reasoning": f"Triple oversold: RSI {r:.0f} / StochRSI {s:.0f} / MFI {m:.0f}. Three independent oscillators agree."}
    return None
