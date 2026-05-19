"""CONFIRM LONG when cross-asset vol is broadly low (risk-on tape)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "macro_low_vol_riskon"
DESCRIPTION = "CONFIRM LONG when cross-asset vol is broadly low (risk-on)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    cav = ((alt.get("macro") or {}).get("cross_asset_vol")) or {}
    if not cav:
        return None
    low_count = 0
    for key in ("move", "ovx", "gvz"):
        sub = cav.get(key) or {}
        if (sub.get("p30d_label") or "").lower() in ("low", "very low"):
            low_count += 1
    if low_count >= 2:
        return {"severity": "CONFIRM",
                "reasoning": f"{low_count}/3 cross-asset vol gauges in LOW regime — risk-on tape favors directional longs."}
    return None
