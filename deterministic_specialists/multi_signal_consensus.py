"""CONFIRM signal when 3+ underlying screens agree (score 3+/4)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "multi_signal_consensus"
DESCRIPTION = "CONFIRM when ensemble score ≥ 3 (3+ underlying screens agree)"
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
    if s >= 3:
        return {"severity": "CONFIRM",
                "reasoning": f"Ensemble score {s:.0f}/4 — multiple independent screens agree."}
    return None
