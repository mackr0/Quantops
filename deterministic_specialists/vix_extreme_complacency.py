"""CAUTION LONG when VIX < 11 (extreme complacency, mean-reversion risk)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "vix_extreme_complacency"
DESCRIPTION = "CAUTION LONG when VIX < 11 (complacency, mean-reversion risk)"
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
    if v >= 11:
        return None
    return {"severity": "CAUTION",
            "reasoning": f"VIX {v:.1f} — extreme complacency. VIX mean-reversion historically catches longs off-guard."}
