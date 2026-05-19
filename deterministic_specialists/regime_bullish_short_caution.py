"""CAUTION SHORT when market regime is bullish."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "regime_bullish_short_caution"
DESCRIPTION = "CAUTION SHORT when market regime is bullish"
APPLIES_TO_SIGNALS = ("SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")
_BULL = ("bull", "bullish", "uptrend", "trending_up", "risk_on")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    mc = candidate.get("_market_context") or {}
    regime = (mc.get("regime") or "").lower()
    if regime not in _BULL:
        return None
    return {"severity": "CAUTION",
            "reasoning": f"Market regime '{regime}' — SHORT fights the prevailing tape; needs specific catalyst."}
