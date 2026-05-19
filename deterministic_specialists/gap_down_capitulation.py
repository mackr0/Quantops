"""CONFIRM LONG when a heavy gap-down (<-3%) coincides with RSI
oversold (<35) — capitulation bounce setup."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "gap_down_capitulation"
DESCRIPTION = "CONFIRM LONG on gap < -3% AND RSI < 35 (capitulation bounce)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    gap = candidate.get("gap_pct")
    rsi = candidate.get("rsi")
    if gap is None or rsi is None:
        return None
    try:
        g = float(gap); r = float(rsi)
    except (TypeError, ValueError):
        return None
    if g < -3.0 and r < 35:
        return {"severity": "CONFIRM",
                "reasoning": f"Gap {g:.1f}% + RSI {r:.0f} — capitulation bounce setup."}
    return None
