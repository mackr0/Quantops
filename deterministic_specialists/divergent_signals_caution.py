"""CAUTION on directional signals when underlying screens disagree
(low score + reason text claims a strong pattern)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "divergent_signals_caution"
DESCRIPTION = "CAUTION when score is low but reason text claims a strong pattern"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")
_STRONG_CLAIMS = ("strong", "breakout", "surge", "spike", "explosive")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    score = candidate.get("score")
    reason = (candidate.get("reason") or "").lower()
    if score is None or not reason:
        return None
    try:
        s = float(score)
    except (TypeError, ValueError):
        return None
    if s > 1:
        return None
    if not any(kw in reason for kw in _STRONG_CLAIMS):
        return None
    return {"severity": "CAUTION",
            "reasoning": f"Reason text claims a strong pattern but ensemble score is only {s:.0f}/4 — other screens don't confirm."}
