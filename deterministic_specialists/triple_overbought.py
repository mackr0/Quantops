"""VETO LONG when RSI, StochRSI, AND MFI all in overbought zone.

Three independent oscillators agreeing is the classic "this is
overdone in every dimension" stop sign. Mean reversion is the
overwhelming base case.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "triple_overbought"
DESCRIPTION = "VETO LONG when RSI ≥ 75 + StochRSI ≥ 80 + MFI ≥ 80"
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
    if r >= 75 and s >= 80 and m >= 80:
        return {"severity": "VETO",
                "reasoning": f"Triple overbought: RSI {r:.0f} / StochRSI {s:.0f} / MFI {m:.0f}. Three independent oscillators agree."}
    return None
