"""CONFIRM LONG when trailing PE is in the value zone (5-15) AND
the company is profitable."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "pe_value_zone"
DESCRIPTION = "CONFIRM LONG when PE 5-15 AND profitable"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


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
    if 5 <= v <= 15:
        return {"severity": "CONFIRM",
                "reasoning": f"Trailing PE {v:.1f} — classic value zone with positive earnings."}
    return None
