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
    # No news + no SEC alert + no near-term earnings = no clear catalyst.
    if news or sec.get("severity") or has_earnings <= 7:
        return None
    # Narrowed 2026-05-18 PM (post-Phase-3 audit). Absence-of-catalyst
    # alone was firing on most stable LONG candidates and biasing the
    # panel against routine entries. Now ALSO requires the move to be
    # mechanically suspicious — high ROC10 (>5%) without a catalyst
    # reason is the actual concerning case. Pure-technical entries on
    # stable names with normal indicators don't need this caution.
    roc = candidate.get("roc_10")
    try:
        roc_f = float(roc) if roc is not None else 0.0
    except (TypeError, ValueError):
        roc_f = 0.0
    if abs(roc_f) < 5.0:
        return None
    return {"severity": "CAUTION",
            "reasoning": f"Strong move (ROC10 {roc_f:+.1f}%) with no news, SEC alert, or near earnings. Driver is unclear — often sympathy/rotation."}
