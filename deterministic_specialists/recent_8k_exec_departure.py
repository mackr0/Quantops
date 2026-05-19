"""CAUTION LONG when a recent 8-K Item 5.02 was filed (executive
officer / director departure or appointment).

5.02 alone can be benign (planned retirement, scheduled rotation)
or severe (forced resignation, CFO out). Surface as CAUTION so
the AI weighs the broader narrative — not a VETO because half of
5.02 events are routine.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "recent_8k_exec_departure"
DESCRIPTION = "CAUTION LONG on recent 8-K Item 5.02 (exec departure/appointment)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    events_block = alt.get("recent_8k_events") or {}
    events = events_block.get("events") or []
    for ev in events:
        if "5.02" in [str(t) for t in (ev.get("item_tags") or [])]:
            return {
                "severity": "CAUTION",
                "reasoning": (
                    "Recent 8-K Item 5.02 (exec departure/appointment). "
                    "Half are routine, half are forced — weigh narrative."
                ),
            }
    return None
