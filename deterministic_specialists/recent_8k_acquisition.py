"""CAUTION on either side after 8-K Item 1.01 (material definitive
agreement — typically M&A or strategic deal)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "recent_8k_acquisition"
DESCRIPTION = "CAUTION on recent 8-K Item 1.01 (material definitive agreement)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    events_block = alt.get("recent_8k_events") or {}
    for ev in (events_block.get("events") or []):
        tags = [str(t) for t in (ev.get("item_tags") or [])]
        if "1.01" in tags:
            return {"severity": "CAUTION",
                    "reasoning": "Recent 8-K Item 1.01 (material definitive agreement). Often M&A — read before sizing; reaction can swing either way."}
    return None
