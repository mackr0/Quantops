"""CAUTION LONG / CONFIRM SHORT on a shooting-star candle
(small body at bottom of range + long upper wick).

Mirror of the hammer. Buyers pushed price up intra-bar but sellers
absorbed and closed near the low — typical exhaustion at the top
of a rally.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "candle_shooting_star"
DESCRIPTION = "CAUTION LONG on shooting-star candle (long upper wick + small bottom body)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    c = (candidate.get("candle") or {}).get("today") or {}
    if not c or c.get("rng", 0) <= 0:
        return None
    body = c.get("body_pct", 0)
    upper = c.get("upper_wick_pct", 0)
    lower = c.get("lower_wick_pct", 0)
    if body < 0.30 and upper >= 0.60 and lower < 0.15:
        color = "green" if c.get("is_green") else "red"
        return {"severity": "CAUTION",
                "reasoning": f"Shooting-star candle ({color} body {body:.0%}, upper wick {upper:.0%}). Buyers exhausted intra-bar; close near low."}
    return None
