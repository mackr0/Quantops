"""CONFIRM SHORT on a strong red marubozu — body fills most of
the range, close near the low."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "candle_marubozu_short"
DESCRIPTION = "CONFIRM SHORT on red marubozu (body >= 80% of range)"
APPLIES_TO_SIGNALS = ("SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    c = (candidate.get("candle") or {}).get("today") or {}
    if not c or c.get("rng", 0) <= 0:
        return None
    body = c.get("body_pct", 0)
    close_pos = c.get("close_to_high_pct", 1.0)
    if not c.get("is_green") and body >= 0.80 and close_pos <= 0.05:
        return {"severity": "CONFIRM",
                "reasoning": f"Red marubozu (body {body:.0%}, close at bottom of range). Sustained directional selling — trend-day signature."}
    return None
