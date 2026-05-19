"""CAUTION on breakout signals when ADX < 20 (no trend).

ADX measures trend strength regardless of direction. < 20 means
the market is range-bound. Breakout signals in low-ADX environments
have low follow-through and tend to fail back into the range.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "weak_adx_breakout"
DESCRIPTION = "CAUTION on breakouts when ADX < 20 (no trend backdrop)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")
_BREAKOUT_KW = ("breakout", "break out", "broke out", "squeeze", "momentum")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    reason = (candidate.get("reason") or "").lower()
    if not any(kw in reason for kw in _BREAKOUT_KW):
        return None
    adx = candidate.get("adx")
    if adx is None:
        return None
    try:
        a = float(adx)
    except (TypeError, ValueError):
        return None
    if a < 20:
        return {
            "severity": "CAUTION",
            "reasoning": (
                f"Breakout/momentum signal fires but ADX is {a:.0f} "
                "(<20 = range-bound). Low-trend breakouts fail back."
            ),
        }
    return None
