"""VETO LONG when VIX > 35 (acute risk-off / panic regime)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "vix_extreme_panic"
DESCRIPTION = "VETO LONG when VIX > 35 (acute panic regime)"
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
    if v <= 35:
        return None
    return {"severity": "VETO",
            "reasoning": f"VIX {v:.1f} — acute panic regime. Even strong setups get re-correlated to the index in this state."}
