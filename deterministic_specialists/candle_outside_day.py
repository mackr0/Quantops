"""CONFIRM signal on an outside-day bar in the direction of the
close (high > prior high AND low < prior low — wide-range
expansion bar)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "candle_outside_day"
DESCRIPTION = "CONFIRM signal on outside-day (range expansion bar)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    candle = candidate.get("candle") or {}
    today = candle.get("today") or {}
    prior = candle.get("prior") or {}
    if not today or not prior:
        return None
    if today.get("high", 0) <= prior.get("high", 0) or \
       today.get("low", 0) >= prior.get("low", 0):
        return None
    # Outside day — direction-tag based on close color
    sig = (candidate.get("signal") or "").upper()
    is_green = today.get("is_green", False)
    long_sigs = {"BUY", "STRONG_BUY", "WEAK_BUY"}
    short_sigs = {"SELL", "STRONG_SELL", "WEAK_SELL", "SHORT"}
    aligned = (sig in long_sigs and is_green) or (sig in short_sigs and not is_green)
    if not aligned:
        return None
    color = "green" if is_green else "red"
    return {"severity": "CONFIRM",
            "reasoning": f"Outside-day {color} bar — range expansion in the signal direction. Decisive intraday move."}
