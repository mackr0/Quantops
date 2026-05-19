"""CONFIRM LONG when TTM-squeeze fires AND ensemble score ≥ 3
(multi-screen agreement on direction at the moment of expansion)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "squeeze_with_consensus"
DESCRIPTION = "CONFIRM LONG on squeeze + ensemble score ≥ 3"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    if not candidate.get("squeeze"):
        return None
    score = candidate.get("score")
    if score is None:
        return None
    try:
        s = float(score)
    except (TypeError, ValueError):
        return None
    if s < 3:
        return None
    return {"severity": "CONFIRM",
            "reasoning": f"Squeeze firing AND ensemble score {s:.0f}/4 — multi-screen consensus at the moment of expansion."}
