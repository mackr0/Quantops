"""CONFIRM LONG when sector is trending up AND stock RS is positive
(aligned with the strongest part of the market)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "sector_strength_aligned"
DESCRIPTION = "CONFIRM LONG when sector trending up AND stock RS positive"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    rs = candidate.get("rel_strength")
    if not isinstance(rs, dict):
        return None
    trend = (rs.get("sector_trend") or "").lower()
    if trend not in ("up", "uptrend", "bullish"):
        return None
    try:
        rsv = float(rs.get("relative_strength", 0))
    except (TypeError, ValueError):
        return None
    if rsv <= 0:
        return None
    return {"severity": "CONFIRM",
            "reasoning": f"{rs.get('sector', '?')} sector trending up + stock RS +{rsv:.1f}%. Aligned with the strongest market segment."}
