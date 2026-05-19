"""CAUTION LONG / CONFIRM SHORT on three black crows — 3
consecutive red bars, each closing lower than the prior."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "candle_three_black_crows"
DESCRIPTION = "CAUTION LONG on 3 consecutive lower-close red bars"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    candle = candidate.get("candle") or {}
    today = candle.get("today") or {}
    prior = candle.get("prior") or {}
    prior2 = candle.get("prior2") or {}
    if not (today and prior and prior2):
        return None
    if today.get("is_green") or prior.get("is_green") or prior2.get("is_green"):
        return None
    if not (today.get("close", 0) < prior.get("close", 0) < prior2.get("close", 0)):
        return None
    return {"severity": "CAUTION",
            "reasoning": "Three black crows — 3 consecutive red bars each closing below the prior. Strong downward momentum; LONG fights the tape."}
