"""CONFIRM LONG on Wednesday entries (documented weekday-of-week
effect: Wednesday has the highest mid-week return base rate)."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

NAME = "wednesday_strength"
DESCRIPTION = "CONFIRM LONG on Wednesday entries (weekday effect)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    et = datetime.now(tz=timezone.utc) - timedelta(hours=4)
    if et.weekday() != 2:  # Wednesday only
        return None
    return {"severity": "CONFIRM",
            "reasoning": "Wednesday — documented mid-week strength bias for US equities."}
