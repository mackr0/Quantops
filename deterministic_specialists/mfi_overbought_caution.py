"""CAUTION LONG when Money Flow Index ≥ 80 (overbought on volume-
weighted RSI).

MFI is the volume-weighted RSI. >80 means the move has been not
just price-strong but volume-strong — typically a late-cycle
indicator. Combined with already-elevated RSI it's a piling-in
warning.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "mfi_overbought_caution"
DESCRIPTION = "CAUTION LONG when MFI ≥ 80 (volume-weighted overbought)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    mfi = candidate.get("mfi")
    if mfi is None:
        return None
    try:
        v = float(mfi)
    except (TypeError, ValueError):
        return None
    if v < 80:
        return None
    return {
        "severity": "CAUTION",
        "reasoning": (
            f"MFI {v:.0f} ≥ 80 — volume-weighted overbought. Late-cycle "
            "piling-in pattern."
        ),
    }
