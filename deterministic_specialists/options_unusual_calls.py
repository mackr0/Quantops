"""CONFIRM LONG when unusual call activity is detected (call-skew
specifically)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "options_unusual_calls"
DESCRIPTION = "CONFIRM LONG when unusual options flow is call-heavy (P/C < 0.6)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    opts = alt.get("options") or {}
    if not opts.get("unusual"):
        return None
    pcr = opts.get("put_call_ratio")
    if pcr is None:
        return None
    try:
        v = float(pcr)
    except (TypeError, ValueError):
        return None
    if v >= 0.6:
        return None
    return {"severity": "CONFIRM",
            "reasoning": f"Unusual options flow call-heavy (P/C {v:.2f}). Smart money buying upside calls."}
