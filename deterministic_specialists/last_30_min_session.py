"""CAUTION on entries in the last 30 minutes of the session (MOC
imbalance chaos + holding overnight risk)."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

NAME = "last_30_min_session"
DESCRIPTION = "CAUTION on entries after 15:30 ET (MOC chaos + overnight risk)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    et = datetime.now(tz=timezone.utc) - timedelta(hours=4)
    if et.weekday() >= 5:
        return None
    minute_of_day = et.hour * 60 + et.minute
    if 15*60 + 30 <= minute_of_day <= 16*60:
        return {"severity": "CAUTION",
                "reasoning": "Last 30 min of session — MOC imbalances dominate price; entries here carry overnight risk too."}
    return None
