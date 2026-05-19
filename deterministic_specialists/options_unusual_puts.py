"""CAUTION LONG when unusual put activity is detected (put-skew)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "options_unusual_puts"
DESCRIPTION = "CAUTION LONG when unusual options flow is put-heavy (P/C > 1.5)"
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
    if v <= 1.5:
        return None
    return {"severity": "CAUTION",
            "reasoning": f"Unusual options flow put-heavy (P/C {v:.2f}). Smart money buying downside puts — fighting the BUY."}
