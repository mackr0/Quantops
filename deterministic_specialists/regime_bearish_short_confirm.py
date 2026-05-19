"""CONFIRM SHORT when market regime is bearish."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "regime_bearish_short_confirm"
DESCRIPTION = "CONFIRM SHORT when market regime is bearish"
APPLIES_TO_SIGNALS = ("SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")
_BEAR = ("bear", "bearish", "downtrend", "trending_down", "risk_off")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    mc = candidate.get("_market_context") or {}
    regime = (mc.get("regime") or "").lower()
    if regime not in _BEAR:
        return None
    return {"severity": "CONFIRM",
            "reasoning": f"Market regime '{regime}' — SHORT aligns with the prevailing tape."}
