"""CAUTION SHORT when PE is in the value zone (5-15) — value
stocks have asymmetric upside on the slightest catalyst."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "value_short_warning"
DESCRIPTION = "CAUTION SHORT when PE in value zone (asymmetric upside risk)"
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
    if 5 <= v <= 15:
        return {"severity": "CAUTION",
                "reasoning": f"PE {v:.1f} in value zone — asymmetric upside risk on the slightest positive catalyst."}
    return None
