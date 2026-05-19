"""CAUTION LONG / CONFIRM SHORT on a bearish-engulfing 2-bar pattern:
yesterday's green bar is fully contained inside today's red bar."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "candle_bearish_engulfing"
DESCRIPTION = "CAUTION LONG on bearish-engulfing 2-bar pattern"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    candle = candidate.get("candle") or {}
    today = candle.get("today") or {}
    prior = candle.get("prior") or {}
    if not today or not prior:
        return None
    if today.get("is_green") or not prior.get("is_green"):
        return None  # need red today, green yesterday
    t_open = today.get("open", 0)
    t_close = today.get("close", 0)
    p_open = prior.get("open", 0)
    p_close = prior.get("close", 0)
    # Today's body engulfs yesterday's body in the opposite direction
    if t_open >= p_close and t_close <= p_open and t_close < t_open:
        return {"severity": "CAUTION",
                "reasoning": "Bearish engulfing — today's red body fully contains yesterday's green body. Strong 2-bar reversal pattern."}
    return None
