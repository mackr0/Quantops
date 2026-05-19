"""CONFIRM LONG when MFI ≤ 20 (volume-weighted oversold).

Mirror of `mfi_overbought_caution`. Oversold on volume-weighted
basis means real selling exhaustion (not just price drift),
historically a higher-quality reversal signal than RSI alone.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "mfi_oversold_confirm"
DESCRIPTION = "CONFIRM LONG when MFI ≤ 20 (volume-weighted oversold)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    mfi = candidate.get("mfi")
    if mfi is None:
        return None
    try:
        v = float(mfi)
    except (TypeError, ValueError):
        return None
    if v > 20:
        return None
    return {
        "severity": "CONFIRM",
        "reasoning": (
            f"MFI {v:.0f} ≤ 20 — volume-weighted oversold. Real "
            "selling exhaustion, not just price drift."
        ),
    }
