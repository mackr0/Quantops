"""CAUTION when reason text mentions recent wash trade / wash
cycle. Wash-rule cooldowns mean re-entering too quickly creates
disallowed-loss complications even on a winning re-entry."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "wash_cycle_recent"
DESCRIPTION = "CAUTION when reason text mentions recent wash cycle"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")
_WASH_KW = ("wash", "wash sale", "wash cycle", "wash rule", "disallowed")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    reason = (candidate.get("reason") or "").lower()
    if any(kw in reason for kw in _WASH_KW):
        return {"severity": "CAUTION",
                "reasoning": "Reason text flags wash-sale context — re-entering during the 30-day window creates disallowed-loss complications."}
    return None
