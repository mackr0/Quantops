"""CONFIRM signal when ADX > 30 (strong trend) backs the direction.

ADX > 30 indicates a defined trend. Entering with the trend in
these conditions has historically had the best follow-through.
This is the positive corollary to `weak_adx_breakout`.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "strong_adx_trend_confirm"
DESCRIPTION = "CONFIRM signal when ADX > 30 (strong trend backdrop)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    adx = candidate.get("adx")
    if adx is None:
        return None
    try:
        a = float(adx)
    except (TypeError, ValueError):
        return None
    if a >= 30:
        return {
            "severity": "CONFIRM",
            "reasoning": (
                f"ADX {a:.0f} ≥ 30 — strong trend backdrop. "
                "Trend-following entries have best follow-through here."
            ),
        }
    return None
