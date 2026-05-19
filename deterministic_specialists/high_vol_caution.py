"""CAUTION on entries when ATR% > 5% (high-vol regime — size down)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "high_vol_caution"
DESCRIPTION = "CAUTION when ATR% > 5% (high-vol regime — size down)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    atr_pct = candidate.get("atr_pct")
    if atr_pct is None:
        return None
    try:
        v = float(atr_pct)
    except (TypeError, ValueError):
        return None
    if v > 5:
        return {"severity": "CAUTION",
                "reasoning": f"ATR% {v:.1f}% — high-vol regime. Stops + sizing have to account for daily 5%+ moves."}
    return None
