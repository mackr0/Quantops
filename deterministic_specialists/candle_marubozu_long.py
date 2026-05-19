"""CONFIRM LONG on a strong green marubozu — body fills most of
the range, close near the high.

Wide green body with tiny wicks = sustained directional buying
all session. One of the cleanest "trend day" candles.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "candle_marubozu_long"
DESCRIPTION = "CONFIRM LONG on green marubozu (body >= 80% of range)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    c = (candidate.get("candle") or {}).get("today") or {}
    if not c or c.get("rng", 0) <= 0:
        return None
    body = c.get("body_pct", 0)
    close_pos = c.get("close_to_high_pct", 0)
    if c.get("is_green") and body >= 0.80 and close_pos >= 0.95:
        return {"severity": "CONFIRM",
                "reasoning": f"Green marubozu (body {body:.0%}, close at top of range). Sustained directional buying — trend-day signature."}
    return None
