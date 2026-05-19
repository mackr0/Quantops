"""CAUTION LONG when insiders sold within 30 days of earnings."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "insider_selling_near_earnings"
DESCRIPTION = "CAUTION LONG when insiders sold near earnings"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    ie = alt.get("insider_earnings") or {}
    if not ie.get("insider_selling_near_earnings"):
        return None
    d2e = ie.get("days_to_earnings", "?")
    return {"severity": "CAUTION",
            "reasoning": f"Insiders sold {d2e}d before earnings — bearish signal even if cleared by counsel."}
