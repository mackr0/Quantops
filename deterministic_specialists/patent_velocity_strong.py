"""CONFIRM LONG when patent filing velocity is accelerating
(innovation pipeline strong)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "patent_velocity_strong"
DESCRIPTION = "CONFIRM LONG when patent filing velocity is accelerating"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    p = alt.get("patent_activity") or {}
    if not p.get("has_data"):
        return None
    trend = (p.get("velocity_trend") or "").lower()
    if trend not in ("accelerating", "rising", "up"):
        return None
    rec90 = p.get("recent_filings_90d", 0)
    rec365 = p.get("recent_filings_365d", 0)
    return {"severity": "CONFIRM",
            "reasoning": f"Patent velocity {trend} ({rec90}/90d, {rec365}/yr). R&D pipeline accelerating."}
