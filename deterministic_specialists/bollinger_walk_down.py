"""CONFIRM SHORT on a Bollinger-walk-down signature: RSI < 40,
ADX > 25, price below VWAP. Strong downtrends ride the lower band."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "bollinger_walk_down"
DESCRIPTION = "CONFIRM SHORT on Bollinger-walk-down signature"
APPLIES_TO_SIGNALS = ("SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


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
    if 22 <= r <= 40 and a > 25 and v < 0:
        return {"severity": "CONFIRM",
                "reasoning": f"Bollinger-walk-down signature: RSI {r:.0f} + ADX {a:.0f} + below VWAP. Trend names ride the lower band."}
    return None
