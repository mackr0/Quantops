"""CAUTION on entries when cross-asset vol is elevated.

When MOVE (bond vol), OVX (oil vol), or GVZ (gold vol) is in the
high regime, broad risk-off is in motion. Single-stock entries
work less well when the macro tide is going out.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "macro_risk_off_cross_asset_vol"
DESCRIPTION = "CAUTION when cross-asset vol (MOVE/OVX/GVZ) is high"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    macro = alt.get("macro") or {}
    cav = macro.get("cross_asset_vol") or {}
    if not cav:
        return None
    high = []
    for key in ("move", "ovx", "gvz"):
        sub = cav.get(key) or {}
        label = sub.get("p30d_label", "") or ""
        if label.lower() in ("high", "very high", "extreme"):
            high.append(f"{key.upper()}={label}")
    if not high:
        return None
    return {
        "severity": "CAUTION",
        "reasoning": (
            f"Cross-asset vol elevated: {', '.join(high)}. Macro "
            "risk-off in progress; single-stock edge compresses."
        ),
    }
