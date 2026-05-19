"""CAUTION on entries in the first 5 minutes after the open
(opening-auction overshoot, spreads wide, fills bad)."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

NAME = "first_5_min_session"
DESCRIPTION = "CAUTION on entries in the first 5 min after the open"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    et = datetime.now(tz=timezone.utc) - timedelta(hours=4)
    if et.weekday() >= 5:
        return None
    minute_of_day = et.hour * 60 + et.minute
    if 9*60 + 30 <= minute_of_day <= 9*60 + 35:
        return {"severity": "CAUTION",
                "reasoning": "First 5 min of session — opening-auction overshoot + wide spreads. Wait for the algos to settle."}
    return None
