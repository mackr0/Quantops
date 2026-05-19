"""CONFIRM LONG when VIX < 18 (low-vol risk-on regime)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "vix_low_riskon"
DESCRIPTION = "CONFIRM LONG when VIX < 18 (low-vol risk-on regime)"
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
    if v >= 18 or v < 11:
        return None  # 11-18 is the productive low-vol band; below 11 → complacency rule fires
    return {"severity": "CONFIRM",
            "reasoning": f"VIX {v:.1f} — low-vol risk-on regime. Directional longs have a tailwind from broad index drift."}
