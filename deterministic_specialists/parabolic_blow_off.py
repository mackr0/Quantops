"""VETO LONG on parabolic blow-off conditions.

Definition: ROC10 > 15% AND RSI > 85 → the move is vertical and
overbought on a multi-week basis. Mean reversion is the base case;
chasing here is the classic "blow-off top" entry that exits at
the wick.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "parabolic_blow_off"
DESCRIPTION = "VETO LONG on parabolic blow-off (ROC10>15% AND RSI>85)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    rsi = candidate.get("rsi")
    roc = candidate.get("roc_10")
    if rsi is None or roc is None:
        return None
    try:
        rsi_f = float(rsi)
        roc_f = float(roc)
    except (TypeError, ValueError):
        return None
    if rsi_f > 85 and roc_f > 15:
        return {
            "severity": "VETO",
            "reasoning": (
                f"Parabolic blow-off: ROC10 {roc_f:+.1f}% and RSI {rsi_f:.0f}. "
                "Mean reversion is the base case; chasing here is buying the wick."
            ),
        }
    return None
