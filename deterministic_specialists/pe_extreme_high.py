"""CAUTION LONG when trailing PE > 50 (extreme valuation)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "pe_extreme_high"
DESCRIPTION = "CAUTION LONG when PE > 50 (extreme valuation)"
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
    if v > 50:
        return {"severity": "CAUTION",
                "reasoning": f"Trailing PE {v:.1f} (>50) — margin for execution error narrow."}
    return None
