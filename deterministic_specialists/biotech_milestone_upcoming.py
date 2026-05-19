"""CAUTION on either-side entries when a biotech catalyst is
upcoming (PDUFA, trial readout) — binary risk."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "biotech_milestone_upcoming"
DESCRIPTION = "CAUTION on biotech catalyst upcoming (PDUFA/readout)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    bm = alt.get("biotech_milestones") or {}
    if not bm.get("has_upcoming"):
        return None
    days = bm.get("days_to_event") or "?"
    kind = bm.get("event_type") or "catalyst"
    return {"severity": "CAUTION",
            "reasoning": f"Biotech {kind} in {days}d — binary outcome. Position-size for tail risk."}
