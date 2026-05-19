"""CONFIRM LONG on morning-star — 3-bar reversal pattern.

Day 1: long red (decisive selling)
Day 2: small body (doji-like — indecision after the sell-off)
Day 3: long green closing above day-1 midpoint (buyers reclaim)

One of the most well-documented 3-bar reversal setups.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "candle_morning_star"
DESCRIPTION = "CONFIRM LONG on morning-star 3-bar reversal"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    candle = candidate.get("candle") or {}
    today = candle.get("today") or {}
    prior = candle.get("prior") or {}
    prior2 = candle.get("prior2") or {}
    if not (today and prior and prior2):
        return None
    # Day 1 (prior2): long red body
    if prior2.get("is_green") or prior2.get("body_pct", 0) < 0.50:
        return None
    # Day 2 (prior): small body — indecision
    if prior.get("body_pct", 0) > 0.30:
        return None
    # Day 3 (today): long green, closing above day-1 midpoint
    if not today.get("is_green") or today.get("body_pct", 0) < 0.50:
        return None
    p2_open = prior2.get("open", 0)
    p2_close = prior2.get("close", 0)
    midpoint = (p2_open + p2_close) / 2
    if today.get("close", 0) < midpoint:
        return None
    return {"severity": "CONFIRM",
            "reasoning": "Morning star — 3-bar reversal: decisive red → small indecision → strong green reclaiming midpoint of the sell-off bar."}
