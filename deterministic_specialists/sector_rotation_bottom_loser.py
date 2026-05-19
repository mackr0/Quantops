"""CAUTION LONG when the candidate's sector is a bottom-2 sector
in the recent rotation table."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "sector_rotation_bottom_loser"
DESCRIPTION = "CAUTION LONG when candidate's sector is bottom-2 in sector rotation"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    mc = candidate.get("_market_context") or {}
    rot = mc.get("sector_rotation") or {}
    if not isinstance(rot, dict) or not rot:
        return None
    rs = candidate.get("rel_strength") or {}
    sector = rs.get("sector") if isinstance(rs, dict) else None
    if not sector:
        return None
    items = []
    for k, v in rot.items():
        try:
            items.append((k, float(v.get("5d") if isinstance(v, dict) else v)))
        except (TypeError, ValueError):
            continue
    if not items:
        return None
    items.sort(key=lambda x: x[1])
    bottom2_names = {n.lower() for n, _ in items[:2]}
    if sector.lower() not in bottom2_names:
        return None
    return {"severity": "CAUTION",
            "reasoning": f"{sector} is a bottom-2 sector in recent 5d rotation. Money rotating OUT of the sector; LONG fights flow."}
