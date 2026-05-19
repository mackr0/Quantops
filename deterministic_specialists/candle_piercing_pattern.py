"""CONFIRM LONG on a piercing pattern — yesterday red, today green
opening below yesterday's low but closing back above the midpoint
of yesterday's body. Partial-engulfing reversal."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "candle_piercing_pattern"
DESCRIPTION = "CONFIRM LONG on piercing pattern (partial bullish engulfing)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    candle = candidate.get("candle") or {}
    today = candle.get("today") or {}
    prior = candle.get("prior") or {}
    if not today or not prior:
        return None
    if not today.get("is_green") or prior.get("is_green"):
        return None
    p_open = prior.get("open", 0)
    p_close = prior.get("close", 0)
    p_low = prior.get("low", 0)
    t_open = today.get("open", 0)
    t_close = today.get("close", 0)
    # Today opened below yesterday's low AND closed above the midpoint
    # of yesterday's RED body (i.e., above (p_open + p_close) / 2).
    if t_open >= p_low:
        return None
    midpoint = (p_open + p_close) / 2
    if t_close >= midpoint and t_close < p_open:
        return {"severity": "CONFIRM",
                "reasoning": "Piercing pattern — today gapped below yesterday's low but closed back inside yesterday's body. Partial reversal."}
    return None
