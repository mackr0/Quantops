"""CONFIRM LONG when SPY is in an uptrend."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "spy_uptrend_long_confirm"
DESCRIPTION = "CONFIRM LONG when SPY is in an uptrend"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")
_UP = ("up", "uptrend", "bullish", "trending_up", "above_50sma")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    mc = candidate.get("_market_context") or {}
    spy = (mc.get("spy_trend") or "").lower()
    if spy not in _UP:
        return None
    return {"severity": "CONFIRM",
            "reasoning": f"SPY trend '{spy}' — index drift supports single-name longs."}
