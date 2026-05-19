"""CONFIRM LONG when the recent insider buyer has a strong
historical track record (their past buys preceded outperformance)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "insider_track_record_strong"
DESCRIPTION = "CONFIRM LONG when insider buyer has strong historical track record"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    itr = alt.get("insider_track_records") or {}
    win_rate = itr.get("avg_win_rate") or itr.get("best_win_rate")
    if win_rate is None:
        return None
    try:
        w = float(win_rate)
    except (TypeError, ValueError):
        return None
    if w >= 0.65:
        return {"severity": "CONFIRM",
                "reasoning": f"Recent insider buyer has historical win rate {w:.0%} — buy is statistically meaningful."}
    return None
