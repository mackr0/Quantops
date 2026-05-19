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
    # Below VWAP but not extreme (extended_above_vwap mirror handles -3%+)
    if -3.0 <= d <= -0.1:
        return {"severity": "CAUTION",
                "reasoning": f"Price {d:.1f}% below session VWAP — algo flow leans bearish intraday."}
    return None
