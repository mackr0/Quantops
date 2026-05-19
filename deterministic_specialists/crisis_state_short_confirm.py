"""CONFIRM SHORT when crisis state is active.

Crises = correlation goes to 1, longs get crushed, shorts get
paid. Tactical SHORT in a crisis regime has historical edge.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "crisis_state_short_confirm"
DESCRIPTION = "CONFIRM SHORT when crisis state is active"
APPLIES_TO_SIGNALS = ("SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    mc = candidate.get("_market_context") or {}
    crisis = mc.get("crisis_context")
    if not crisis or not isinstance(crisis, str):
        return None
    upper = crisis.upper()
    level = ""
    for lvl in ("CATASTROPHIC", "SEVERE", "CRITICAL", "HIGH", "ELEVATED"):
        if lvl in upper:
            level = lvl
            break
    if not level:
        return None
    return {"severity": "CONFIRM",
            "reasoning": f"Crisis state {level} — correlation rises to 1, shorts get paid. Tactical short has historical edge."}
