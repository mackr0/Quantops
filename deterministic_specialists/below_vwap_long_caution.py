"""CAUTION LONG when price is below session VWAP (algo flow bearish)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "below_vwap_long_caution"
DESCRIPTION = "CAUTION LONG when price < 0 below session VWAP"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    v = candidate.get("pct_from_vwap")
    if v is None:
        return None
    try:
        d = float(v)
    except (TypeError, ValueError):
        return None
    # Below VWAP by a meaningful amount. Narrowed 2026-05-18 PM
    # (post-Phase-3 audit) — original -0.1% threshold fired on every
    # pullback-buy, biasing the panel against legitimate entries.
    # Now requires -2% to -3% (still below extended_above_vwap's -3%
    # mirror) to fire — only meaningful intraday weakness.
    if -3.0 <= d <= -2.0:
        return {"severity": "CAUTION",
                "reasoning": f"Price {d:.1f}% below session VWAP — meaningful intraday weakness."}
    return None
