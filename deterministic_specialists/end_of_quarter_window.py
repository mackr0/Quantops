"""CONFIRM LONG in the last 3 trading days of a quarter (window-
dressing bid bias for mega-cap leaders)."""
from __future__ import annotations
from datetime import datetime
from typing import Any, Dict, Optional

NAME = "end_of_quarter_window"
DESCRIPTION = "CONFIRM LONG in last 3 trading days of a quarter (window dressing)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    today = datetime.utcnow().date()
    end_months = (3, 6, 9, 12)
    if today.month not in end_months:
        return None
    # Approximate "last 3 trading days" with calendar days 26-31
    if today.day < 26:
        return None
    return {"severity": "CONFIRM",
            "reasoning": f"End of {today.strftime('%B')} (Q{((today.month-1)//3)+1}). Documented window-dressing bid bias on quality names."}
