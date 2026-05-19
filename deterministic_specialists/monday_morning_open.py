"""CAUTION on all entries Monday before 11:00 ET — historically
weakest hour of the week (post-weekend information dump)."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

NAME = "monday_morning_open"
DESCRIPTION = "CAUTION on entries Monday before 11:00 ET (Monday-morning effect)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    now_utc = datetime.now(tz=timezone.utc)
    # ET is UTC-5 (UTC-4 in DST); approximate with UTC-4 to bias
    # toward the wider window — we'd rather over-fire mildly than miss
    et = now_utc - timedelta(hours=4)
    if et.weekday() != 0:  # Monday only
        return None
    # 09:30 ET open through 11:00 ET window
    minute_of_day = et.hour * 60 + et.minute
    if not (9*60 + 30 <= minute_of_day <= 11*60):
        return None
    return {"severity": "CAUTION",
            "reasoning": "Monday morning before 11:00 ET. Historically the weakest weekday-hour for new entries — post-weekend information dump still digesting."}
