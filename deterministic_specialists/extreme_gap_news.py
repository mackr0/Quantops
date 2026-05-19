"""CAUTION on |gap| > 5% — breaking-news regime, not technical."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "extreme_gap_news"
DESCRIPTION = "CAUTION on |gap| > 5% (breaking-news regime)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    gap = candidate.get("gap_pct")
    if gap is None:
        return None
    try:
        g = float(gap)
    except (TypeError, ValueError):
        return None
    if abs(g) > 5.0:
        direction = "up" if g > 0 else "down"
        return {"severity": "CAUTION",
                "reasoning": f"Extreme gap {g:+.1f}% {direction} — breaking-news regime; technicals are unreliable until the news is read."}
    return None
