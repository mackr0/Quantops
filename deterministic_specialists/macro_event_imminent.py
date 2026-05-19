"""CAUTION on entries when a macro event (FOMC / CPI / NFP) is
imminent. Event volatility dominates technical setup until the
print is digested."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "macro_event_imminent"
DESCRIPTION = "CAUTION on entries within 1 day of FOMC / CPI / NFP"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")
_KEY_EVENTS = ("FOMC", "CPI", "NFP", "JOBS", "PAYROLLS", "RATE DECISION", "FED")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    mc = candidate.get("_market_context") or {}
    block = mc.get("macro_event_block")
    if not block or not isinstance(block, str):
        return None
    upper = block.upper()
    hits = [e for e in _KEY_EVENTS if e in upper]
    if not hits:
        return None
    # Block typically reads "FOMC in 0 days" or "CPI tomorrow" — look
    # for proximity indicators
    if not any(kw in upper for kw in ("TODAY", "TOMORROW", "IN 0", "IN 1 DAY", "IMMINENT")):
        return None
    return {"severity": "CAUTION",
            "reasoning": f"Macro event imminent: {', '.join(sorted(set(hits)))}. Event vol dominates technicals until the print."}
