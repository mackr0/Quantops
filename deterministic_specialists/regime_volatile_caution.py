"""CAUTION on entries when the regime is volatile / chop / crisis."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "regime_volatile_caution"
DESCRIPTION = "CAUTION on entries in volatile / crisis regimes (size down)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")
_VOL = ("volatile", "crisis", "chop", "choppy", "stressed")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    mc = candidate.get("_market_context") or {}
    regime = (mc.get("regime") or "").lower()
    if regime not in _VOL:
        return None
    return {"severity": "CAUTION",
            "reasoning": f"Market regime '{regime}' — wide whipsaw range; size down + tighten stops."}
