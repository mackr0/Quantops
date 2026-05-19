"""CAUTION on entries Friday after 15:00 ET (weekend-risk pricing
+ position-squaring chop)."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

NAME = "friday_close_caution"
DESCRIPTION = "CAUTION on entries Friday after 15:00 ET (weekend risk)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    et = datetime.now(tz=timezone.utc) - timedelta(hours=4)
    if et.weekday() != 4:  # Friday
        return None
    minute_of_day = et.hour * 60 + et.minute
    if minute_of_day < 15 * 60:
        return None
    return {"severity": "CAUTION",
            "reasoning": "Friday after 15:00 ET. Position-squaring chop + weekend-risk premium baked into prices."}
