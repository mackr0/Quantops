"""CONFIRM SHORT when PE > 50 (expensive valuation amplifies
downside on missed expectations)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "expensive_short_confirm"
DESCRIPTION = "CONFIRM SHORT when PE > 50"
APPLIES_TO_SIGNALS = ("SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    f = alt.get("fundamentals") or {}
    pe = f.get("pe_trailing") or f.get("pe_ratio")
    if pe is None:
        return None
    try:
        v = float(pe)
    except (TypeError, ValueError):
        return None
    if v > 50:
        return {"severity": "CONFIRM",
                "reasoning": f"PE {v:.1f} — expensive valuation amplifies downside on any miss."}
    return None
