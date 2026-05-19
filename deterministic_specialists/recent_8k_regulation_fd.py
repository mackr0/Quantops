"""CAUTION on entries after 8-K Item 7.01 (Regulation FD —
selective disclosure / earnings preannouncement)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "recent_8k_regulation_fd"
DESCRIPTION = "CAUTION on recent 8-K Item 7.01 (Regulation FD disclosure)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    events_block = alt.get("recent_8k_events") or {}
    for ev in (events_block.get("events") or []):
        tags = [str(t) for t in (ev.get("item_tags") or [])]
        if "7.01" in tags:
            return {"severity": "CAUTION",
                    "reasoning": "Recent 8-K Item 7.01 (Reg FD). Selective disclosure or earnings preannouncement — read context before sizing."}
    return None
