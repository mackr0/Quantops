"""CONFIRM LONG on a hammer candle (small body at top of range +
long lower wick).

Classic reversal candle at the end of a downtrend: sellers drove
price down intra-bar but buyers absorbed the supply and closed near
the high. Body in the upper third, lower wick ≥ 2× the body.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "candle_hammer"
DESCRIPTION = "CONFIRM LONG on hammer candle (small upper body + long lower wick)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    c = (candidate.get("candle") or {}).get("today") or {}
    if not c or c.get("rng", 0) <= 0:
        return None
    body = c.get("body_pct", 0)
    lower = c.get("lower_wick_pct", 0)
    upper = c.get("upper_wick_pct", 0)
    # Small body (<30% of range), long lower wick (>=60%), short upper wick (<15%)
    if body < 0.30 and lower >= 0.60 and upper < 0.15:
        color = "green" if c.get("is_green") else "red"
        return {"severity": "CONFIRM",
                "reasoning": f"Hammer candle ({color} body {body:.0%}, lower wick {lower:.0%}). Sellers absorbed intra-bar; close near high."}
    return None
