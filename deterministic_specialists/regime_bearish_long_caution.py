"""CAUTION LONG when market regime is bearish."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "regime_bearish_long_caution"
DESCRIPTION = "CAUTION LONG when market regime is bearish"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")
_BEAR = ("bear", "bearish", "downtrend", "trending_down", "risk_off")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    mc = candidate.get("_market_context") or {}
    regime = (mc.get("regime") or "").lower()
    if regime not in _BEAR:
        return None
    return {"severity": "CAUTION",
            "reasoning": f"Market regime '{regime}' — LONG fights the prevailing tape."}
