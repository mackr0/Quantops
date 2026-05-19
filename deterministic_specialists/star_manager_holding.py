"""CONFIRM LONG when a star manager (Buffett, Burry, Ackman class)
holds the name as a top position."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "star_manager_holding"
DESCRIPTION = "CONFIRM LONG when a star manager holds the name"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    sm = alt.get("star_manager_holdings") or {}
    holders = sm.get("holders") or []
    if not holders:
        return None
    names = [h.get("name") for h in holders if h.get("name")][:3]
    if not names:
        return None
    return {"severity": "CONFIRM",
            "reasoning": f"Star-manager holding: {', '.join(names)}. Known long-horizon capital aligned with the thesis."}
