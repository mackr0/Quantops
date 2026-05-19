"""CONFIRM LONG on a bullish-engulfing 2-bar pattern: yesterday's
red bar is fully contained inside today's green bar.

A classic 2-bar reversal — strong buyers absorb yesterday's
sellers' range entirely. Strong base rate when it appears after a
clear downtrend bar.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "candle_bullish_engulfing"
DESCRIPTION = "CONFIRM LONG on bullish-engulfing 2-bar pattern"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    candle = candidate.get("candle") or {}
    today = candle.get("today") or {}
    prior = candle.get("prior") or {}
    if not today or not prior:
        return None
    if not today.get("is_green") or prior.get("is_green"):
        return None  # need green today, red yesterday
    t_open = today.get("open", 0)
    t_close = today.get("close", 0)
    p_open = prior.get("open", 0)
    p_close = prior.get("close", 0)
    # Today's body engulfs yesterday's body
    if t_open <= p_close and t_close >= p_open and t_close > t_open:
        return {"severity": "CONFIRM",
                "reasoning": "Bullish engulfing — today's green body fully contains yesterday's red body. Strong 2-bar reversal pattern."}
    return None
