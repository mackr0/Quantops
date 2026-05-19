"""CAUTION on entries when today is an inside day (high < prior
high AND low > prior low) — coil pattern, breakout direction not
yet established."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "candle_inside_day"
DESCRIPTION = "CAUTION on inside-day entries (consolidation; direction unconfirmed)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    candle = candidate.get("candle") or {}
    today = candle.get("today") or {}
    prior = candle.get("prior") or {}
    if not today or not prior:
        return None
    if today.get("high", 0) < prior.get("high", 0) and \
       today.get("low", 0) > prior.get("low", 0):
        return {"severity": "CAUTION",
                "reasoning": "Inside-day pattern — today's range fully inside yesterday's. Coiling; breakout direction not yet established."}
    return None
