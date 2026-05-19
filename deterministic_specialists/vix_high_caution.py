"""CAUTION LONG when VIX > 25 (elevated broad fear)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "vix_high_caution"
DESCRIPTION = "CAUTION LONG when VIX > 25 (elevated broad fear)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    mc = candidate.get("_market_context") or {}
    vix = mc.get("vix")
    if vix is None:
        return None
    try:
        v = float(vix)
    except (TypeError, ValueError):
        return None
    if v <= 25:
        return None
    return {"severity": "CAUTION",
            "reasoning": f"VIX {v:.1f} — elevated broad fear. Single-name longs face heightened correlation to the index sell-off."}
