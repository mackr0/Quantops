"""CAUTION LONG on evening-star — 3-bar bearish reversal pattern.

Day 1: long green (decisive buying)
Day 2: small body (indecision after the rally)
Day 3: long red closing below day-1 midpoint (sellers reclaim)
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "candle_evening_star"
DESCRIPTION = "CAUTION LONG on evening-star 3-bar bearish reversal"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    candle = candidate.get("candle") or {}
    today = candle.get("today") or {}
    prior = candle.get("prior") or {}
    prior2 = candle.get("prior2") or {}
    if not (today and prior and prior2):
        return None
    if not prior2.get("is_green") or prior2.get("body_pct", 0) < 0.50:
        return None
    if prior.get("body_pct", 0) > 0.30:
        return None
    if today.get("is_green") or today.get("body_pct", 0) < 0.50:
        return None
    p2_open = prior2.get("open", 0)
    p2_close = prior2.get("close", 0)
    midpoint = (p2_open + p2_close) / 2
    if today.get("close", 0) > midpoint:
        return None
    return {"severity": "CAUTION",
            "reasoning": "Evening star — 3-bar bearish reversal: decisive green → small indecision → strong red breaking back below midpoint of the rally bar."}
