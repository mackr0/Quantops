"""CONFIRM LONG when yield curve is steepening (typically marks
end-of-cycle reflation; cyclical names benefit)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "macro_yield_curve_steepening"
DESCRIPTION = "CONFIRM LONG on yield-curve steepening (reflation regime)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    yc = ((alt.get("macro") or {}).get("yield_curve")) or {}
    sig = (yc.get("curve_signal") or "").lower()
    if sig not in ("steepening", "bull_steepening", "normalizing"):
        return None
    return {"severity": "CONFIRM",
            "reasoning": f"Yield curve {sig} — historically marks reflation; cyclical longs benefit."}
