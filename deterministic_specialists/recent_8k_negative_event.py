"""VETO LONG on recent 8-K Item 1.03 (bankruptcy), 4.02 (non-
reliance on prior financials), or 2.06 (material impairment).

These specific 8-K items signal active material corporate stress.
Trading LONG into any of them without a specific recovery thesis
is fighting the disclosure itself.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "recent_8k_negative_event"
DESCRIPTION = "VETO LONG on recent 8-K Items 1.03 / 4.02 / 2.06"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")
_RED_FLAG_TAGS = ("1.03", "4.02", "2.06")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    events_block = alt.get("recent_8k_events") or {}
    events = events_block.get("events") or []
    hit = []
    for ev in events:
        tags = ev.get("item_tags") or []
        for t in tags:
            if str(t) in _RED_FLAG_TAGS:
                hit.append(str(t))
    if not hit:
        return None
    return {
        "severity": "VETO",
        "reasoning": (
            f"Recent 8-K with critical Items: {'/'.join(sorted(set(hit)))}. "
            "These disclosures signal active material corporate stress."
        ),
    }
