"""CAUTION LONG when SPY is in a downtrend."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "spy_downtrend_long_caution"
DESCRIPTION = "CAUTION LONG when SPY is in a downtrend"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")
_DOWN = ("down", "downtrend", "bearish", "trending_down", "below_50sma")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    mc = candidate.get("_market_context") or {}
    spy = (mc.get("spy_trend") or "").lower()
    if spy not in _DOWN:
        return None
    return {"severity": "CAUTION",
            "reasoning": f"SPY trend '{spy}' — single-name LONG fights index drift."}
