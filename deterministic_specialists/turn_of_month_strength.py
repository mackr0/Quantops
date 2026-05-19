"""CONFIRM LONG in the turn-of-month window (last 3 + first 3
trading days of a calendar month — documented positive bias)."""
from __future__ import annotations
from datetime import datetime
from typing import Any, Dict, Optional

NAME = "turn_of_month_strength"
DESCRIPTION = "CONFIRM LONG in turn-of-month window (positive seasonal bias)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    today = datetime.utcnow().date()
    d = today.day
    # Last 3 days of month (calendar proxy) OR first 3 days
    if not (d >= 27 or d <= 3):
        return None
    where = "month-end" if d >= 27 else "month-start"
    return {"severity": "CONFIRM",
            "reasoning": f"Turn-of-month window ({where}). Documented +20bps median 6-day excess return historically."}
