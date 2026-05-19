"""CAUTION on entries within 24h of 8-K Item 2.02 (earnings
release filing). The reaction window is volatile; gap fades
common."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "recent_8k_earnings_release"
DESCRIPTION = "CAUTION on recent 8-K Item 2.02 (earnings release)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    events_block = alt.get("recent_8k_events") or {}
    for ev in (events_block.get("events") or []):
        if "2.02" in [str(t) for t in (ev.get("item_tags") or [])]:
            return {"severity": "CAUTION",
                    "reasoning": "Recent 8-K Item 2.02 (earnings release). Reaction window is volatile; gap-fades common in first 24h."}
    return None
