"""CAUTION LONG on a streak of negative earnings surprises.

Mirror of `earnings_surprise_streak`. Misses cluster too — going
LONG into a 3+ quarter miss streak fights the prevailing earnings
narrative.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "earnings_miss_streak"
DESCRIPTION = "CAUTION LONG when 3+ quarters of earnings misses"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    es = alt.get("earnings_surprise") or {}
    total = es.get("total_quarters", 0) or 0
    beats = es.get("beat_count", 0) or 0
    if total < 4:
        return None
    misses = total - beats
    if misses < 3:
        return None
    avg = es.get("avg_surprise_pct", 0)
    try:
        avg_f = float(avg)
    except (TypeError, ValueError):
        avg_f = 0.0
    return {
        "severity": "CAUTION",
        "reasoning": (
            f"Earnings miss streak: {misses}/{total} misses, "
            f"avg surprise {avg_f:+.1f}%. Misses cluster — LONG fights "
            "the earnings narrative."
        ),
    }
