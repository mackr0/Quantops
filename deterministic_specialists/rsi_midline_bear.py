"""CAUTION LONG when RSI < 45 (below midline = bearish backdrop).

The bearish corollary to `rsi_midline_bull`. RSI sustainably below
midline means downmoves dominate; LONG fights the trend.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "rsi_midline_bear"
DESCRIPTION = "CAUTION LONG when RSI < 45 (below midline = bearish backdrop)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    rsi = candidate.get("rsi")
    if rsi is None:
        return None
    try:
        v = float(rsi)
    except (TypeError, ValueError):
        return None
    # Below 45 but not yet oversold (rsi_oversold_uptrend handles <30)
    if 30 <= v < 45:
        return {"severity": "CAUTION",
                "reasoning": f"RSI {v:.0f} below midline — LONG fights bearish backdrop."}
    return None
