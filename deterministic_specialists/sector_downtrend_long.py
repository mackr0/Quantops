"""CAUTION LONG when the sector itself is in a downtrend.

Even strong-RS individual names fight the sector tape eventually.
Going LONG in a clearly down-trending sector requires either a
contrarian thesis or a near-term catalyst.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "sector_downtrend_long"
DESCRIPTION = "CAUTION LONG when sector is in a clear downtrend"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    rs = candidate.get("rel_strength")
    if not isinstance(rs, dict):
        return None
    trend = (rs.get("sector_trend") or "").lower()
    if trend not in ("down", "downtrend", "bearish"):
        return None
    sector_5d = rs.get("sector_5d", 0)
    return {
        "severity": "CAUTION",
        "reasoning": (
            f"{rs.get('sector', '?')} sector in {trend} "
            f"(5d {sector_5d:+.1f}%). LONG fights the sector tape."
        ),
    }
