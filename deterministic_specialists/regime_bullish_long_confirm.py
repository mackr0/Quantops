"""CONFIRM LONG when market regime is bullish."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "regime_bullish_long_confirm"
DESCRIPTION = "CONFIRM LONG when market regime is bullish"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")
_BULL = ("bull", "bullish", "uptrend", "trending_up", "risk_on")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    mc = candidate.get("_market_context") or {}
    regime = (mc.get("regime") or "").lower()
    if regime not in _BULL:
        return None
    return {"severity": "CONFIRM",
            "reasoning": f"Market regime '{regime}' — directional LONG aligns with the prevailing tape."}
