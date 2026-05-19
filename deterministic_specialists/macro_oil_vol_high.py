"""CAUTION LONG for energy-sensitive names when OVX (oil vol) is
elevated."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "macro_oil_vol_high"
DESCRIPTION = "CAUTION LONG when OVX (oil vol) is in high regime"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    ovx = (((alt.get("macro") or {}).get("cross_asset_vol")) or {}).get("ovx") or {}
    label = (ovx.get("p30d_label") or "").lower()
    if label not in ("high", "very high", "extreme"):
        return None
    return {"severity": "CAUTION",
            "reasoning": f"OVX (oil vol) {label}. Energy-sensitive names face commodity-driven volatility."}
