"""CAUTION LONG when FINRA short-volume ratio is elevated.

A high short-volume ratio (>40% of daily volume getting tagged as
short sells) means active shorting is in motion that day. Going
LONG into active shorting requires either a squeeze catalyst or
a known information asymmetry.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "finra_short_volume_elevated"
DESCRIPTION = "CAUTION LONG when FINRA short volume ratio is elevated"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")
_THRESHOLD = 0.40


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    finra = alt.get("finra_short_vol") or {}
    if not finra.get("is_elevated"):
        return None
    svr = finra.get("short_volume_ratio")
    try:
        v = float(svr) if svr is not None else None
    except (TypeError, ValueError):
        v = None
    detail = f" ({v:.0%} of daily)" if v is not None else ""
    return {
        "severity": "CAUTION",
        "reasoning": (
            f"FINRA short volume elevated{detail}. Active shorting "
            "in progress — LONG fights the daily flow."
        ),
    }
