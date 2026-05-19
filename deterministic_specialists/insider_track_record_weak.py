"""CAUTION LONG when the recent insider buyer has a weak historical
track record (their past buys haven't preceded outperformance)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "insider_track_record_weak"
DESCRIPTION = "CAUTION LONG when insider buyer has poor historical track record"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    itr = alt.get("insider_track_records") or {}
    wr = itr.get("avg_win_rate")
    if wr is None:
        return None
    try:
        w = float(wr)
    except (TypeError, ValueError):
        return None
    if w <= 0.40:
        return {"severity": "CAUTION",
                "reasoning": f"Insider buyer historical win rate {w:.0%} — past buys have not preceded outperformance."}
    return None
