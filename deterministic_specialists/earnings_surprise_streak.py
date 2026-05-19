"""CONFIRM LONG on a streak of positive earnings surprises.

Companies that beat consensus repeatedly tend to keep beating
(earnings momentum is one of the most-cited anomalies — Bernard
& Thomas 1989 PEAD). 4+ quarters of beats with positive avg
surprise is the standard threshold.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "earnings_surprise_streak"
DESCRIPTION = "CONFIRM LONG when 4+ quarters of positive earnings surprises"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    es = alt.get("earnings_surprise") or {}
    total = es.get("total_quarters", 0) or 0
    beats = es.get("beat_count", 0) or 0
    avg = es.get("avg_surprise_pct", 0)
    if total < 4 or beats < 3:
        return None
    try:
        avg_f = float(avg)
    except (TypeError, ValueError):
        return None
    if avg_f <= 0:
        return None
    return {
        "severity": "CONFIRM",
        "reasoning": (
            f"Earnings surprise streak: {beats}/{total} beats, "
            f"avg surprise {avg_f:+.1f}%. PEAD anomaly — beats cluster."
        ),
    }
