"""CAUTION on directional entries when GVZ (gold vol) is elevated
— signals safe-haven repricing / regime change."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "macro_gold_vol_high"
DESCRIPTION = "CAUTION when GVZ (gold vol) is in high regime"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    gvz = (((alt.get("macro") or {}).get("cross_asset_vol")) or {}).get("gvz") or {}
    label = (gvz.get("p30d_label") or "").lower()
    if label not in ("high", "very high", "extreme"):
        return None
    return {"severity": "CAUTION",
            "reasoning": f"GVZ (gold vol) {label}. Safe-haven repricing in motion — regime change risk for equity entries."}
