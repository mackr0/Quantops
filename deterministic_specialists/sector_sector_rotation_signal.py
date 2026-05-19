"""CAUTION LONG when sector is trending UP but stock is materially
underperforming the sector — sector rotation may have left this name
behind."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "sector_sector_rotation_signal"
DESCRIPTION = "CAUTION LONG when sector trending up but stock RS < -5%"
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
    if rsv > -5:
        return None
    return {"severity": "CAUTION",
            "reasoning": f"{rs.get('sector', '?')} sector trending up but stock RS {rsv:+.1f}% — laggard within a winning sector."}
