"""CONFIRM LONG when 10-day ROC > 5% (momentum factor positive)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "momentum_5d_strong_positive"
DESCRIPTION = "CONFIRM LONG when ROC10 > 5% (momentum factor)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    roc = candidate.get("roc_10")
    if roc is None:
        return None
    try:
        v = float(roc)
    except (TypeError, ValueError):
        return None
    if 5 <= v <= 15:  # below parabolic_blow_off territory
        return {"severity": "CONFIRM",
                "reasoning": f"ROC10 {v:+.1f}% — momentum factor positive without parabolic extension."}
    return None
