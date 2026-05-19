"""CAUTION LONG when relative-strength block flags negative
short-side flow even with positive RS — divergent signals."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "sector_high_short_volume"
DESCRIPTION = "CAUTION LONG when stock RS positive but short-vol elevated"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    rs = candidate.get("rel_strength")
    alt = candidate.get("alt_data") or {}
    finra = alt.get("finra_short_vol") or {}
    if not isinstance(rs, dict):
        return None
    try:
        rs_val = float(rs.get("relative_strength", 0))
    except (TypeError, ValueError):
        return None
    if rs_val <= 0:
        return None
    if not finra.get("is_elevated"):
        return None
    return {"severity": "CAUTION",
            "reasoning": f"RS positive (+{rs_val:.1f}%) BUT FINRA short vol elevated — institutions disagreeing with the tape; weigh both sides."}
