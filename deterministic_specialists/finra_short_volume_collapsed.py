"""CONFIRM LONG when FINRA short-volume ratio collapses (shorts
covered en masse — buying pressure)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "finra_short_volume_collapsed"
DESCRIPTION = "CONFIRM LONG when FINRA short-vol ratio < 0.20 (shorts covered)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    finra = alt.get("finra_short_vol") or {}
    svr = finra.get("short_volume_ratio")
    if svr is None:
        return None
    try:
        v = float(svr)
    except (TypeError, ValueError):
        return None
    if v >= 0.20:
        return None
    return {"severity": "CONFIRM",
            "reasoning": f"FINRA short-vol ratio {v:.0%} — shorts covered en masse; structural buy pressure."}
