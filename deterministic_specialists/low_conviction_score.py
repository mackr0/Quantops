"""CAUTION when ensemble score is low (only 1 screen agrees)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "low_conviction_score"
DESCRIPTION = "CAUTION when ensemble score ≤ 1 (low cross-screen agreement)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    score = candidate.get("score")
    if score is None:
        return None
    try:
        s = float(score)
    except (TypeError, ValueError):
        return None
    if s <= 1:
        return {"severity": "CAUTION",
                "reasoning": f"Ensemble score {s:.0f}/4 — only 1 screen agrees; lone-wolf signals have higher failure rate."}
    return None
