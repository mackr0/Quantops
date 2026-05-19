"""CAUTION LONG when the system is in an active crisis state
(crisis_monitor has elevated risk-off signals)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "crisis_state_long_caution"
DESCRIPTION = "CAUTION LONG when crisis state is active"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    mc = candidate.get("_market_context") or {}
    crisis = mc.get("crisis_context")
    if not crisis or not isinstance(crisis, str):
        return None
    # The string includes "CRISIS STATE: LEVEL ..." when active
    level_match = ""
    upper = crisis.upper()
    for lvl in ("CATASTROPHIC", "SEVERE", "CRITICAL", "HIGH", "ELEVATED"):
        if lvl in upper:
            level_match = lvl
            break
    if not level_match:
        return None
    return {"severity": "CAUTION",
            "reasoning": f"Crisis monitor active ({level_match}). System bias toward capital preservation; tighter stops + size down."}
