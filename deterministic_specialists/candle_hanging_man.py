"""CAUTION LONG on a hanging-man candle in an uptrend context.

Same shape as a hammer (small top body + long lower wick) but
appears AFTER a rally — same intra-bar dynamic flips meaning:
buyers no longer in control even though close held the high.
Detected via the hammer shape PLUS positive 10-day momentum.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "candle_hanging_man"
DESCRIPTION = "CAUTION LONG on hanging-man (hammer shape after positive ROC)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    c = (candidate.get("candle") or {}).get("today") or {}
    if not c or c.get("rng", 0) <= 0:
        return None
    body = c.get("body_pct", 0)
    lower = c.get("lower_wick_pct", 0)
    upper = c.get("upper_wick_pct", 0)
    if not (body < 0.30 and lower >= 0.60 and upper < 0.15):
        return None
    # Only fire when in an established uptrend (ROC10 > 3%)
    try:
        roc = float(candidate.get("roc_10", 0))
    except (TypeError, ValueError):
        return None
    if roc <= 3:
        return None
    return {"severity": "CAUTION",
            "reasoning": f"Hanging-man pattern after ROC10 {roc:+.1f}% rally. Same shape as a hammer but in an uptrend = exhaustion warning."}
