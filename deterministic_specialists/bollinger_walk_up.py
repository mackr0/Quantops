"""CONFIRM LONG when in a Bollinger-band-walk-up regime: RSI > 60,
ADX > 25, price above VWAP. Strong trend names ride the upper band
for multi-week runs; this stacks the typical signature."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "bollinger_walk_up"
DESCRIPTION = "CONFIRM LONG on Bollinger-walk-up signature (RSI>60, ADX>25, above VWAP)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    rsi = candidate.get("rsi")
    adx = candidate.get("adx")
    vwap_d = candidate.get("pct_from_vwap")
    if rsi is None or adx is None or vwap_d is None:
        return None
    try:
        r = float(rsi); a = float(adx); v = float(vwap_d)
    except (TypeError, ValueError):
        return None
    if 60 <= r <= 78 and a > 25 and v > 0:
        return {"severity": "CONFIRM",
                "reasoning": f"Bollinger-walk-up signature: RSI {r:.0f} + ADX {a:.0f} + above VWAP. Trend names ride the upper band."}
    return None
