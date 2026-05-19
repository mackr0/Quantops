"""CAUTION LONG when 10-day ROC < -3% (negative momentum)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "momentum_5d_negative_long"
DESCRIPTION = "CAUTION LONG when ROC10 < -3% (negative momentum factor)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    roc = candidate.get("roc_10")
    if roc is None:
        return None
    try:
        v = float(roc)
    except (TypeError, ValueError):
        return None
    if v < -3:
        return {"severity": "CAUTION",
                "reasoning": f"ROC10 {v:+.1f}% — momentum factor against the LONG thesis."}
    return None
