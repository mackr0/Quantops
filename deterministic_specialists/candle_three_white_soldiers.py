"""CONFIRM LONG on three white soldiers — 3 consecutive green
bars, each closing higher than the prior."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "candle_three_white_soldiers"
DESCRIPTION = "CONFIRM LONG on 3 consecutive higher-close green bars"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    candle = candidate.get("candle") or {}
    today = candle.get("today") or {}
    prior = candle.get("prior") or {}
    prior2 = candle.get("prior2") or {}
    if not (today and prior and prior2):
        return None
    if not (today.get("is_green") and prior.get("is_green") and prior2.get("is_green")):
        return None
    if not (today.get("close", 0) > prior.get("close", 0) > prior2.get("close", 0)):
        return None
    return {"severity": "CONFIRM",
            "reasoning": "Three white soldiers — 3 consecutive green bars each closing above the prior. Strong momentum-continuation pattern."}
