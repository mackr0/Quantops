"""CAUTION on directional entries when there's NO recent news AND
NO catalyst — what's driving the move? Often these are sympathy
plays or sector rotation that won't sustain alone."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "no_news_low_attention"
DESCRIPTION = "CAUTION on directional entry with no news + no catalyst signal"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    news = candidate.get("news") or []
    sec = candidate.get("sec_alert") or {}
    alt = candidate.get("alt_data") or {}
    has_earnings = (alt.get("insider_earnings") or {}).get("days_to_earnings", 999)
    try:
        has_earnings = int(has_earnings) if has_earnings is not None else 999
    except (TypeError, ValueError):
        has_earnings = 999
    # No news + no SEC alert + no near-term earnings = no clear catalyst
    if news or sec.get("severity") or has_earnings <= 7:
        return None
    return {"severity": "CAUTION",
            "reasoning": "No news, no SEC alert, no near earnings. What's driving the move? Often sympathy/rotation that won't sustain alone."}
