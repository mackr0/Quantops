"""CONFIRM LONG when realized vol (ATR%) is in the low-vol regime.

Low-vol stocks have historically outperformed risk-adjusted
(Frazzini-Pedersen "Betting Against Beta"). For directional longs,
sub-2% ATR is a sign of a stable name with predictable behavior.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "low_vol_factor"
DESCRIPTION = "CONFIRM LONG when ATR% < 2% (low-vol factor exposure)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    atr_pct = candidate.get("atr_pct")
    if atr_pct is None:
        return None
    try:
        v = float(atr_pct)
    except (TypeError, ValueError):
        return None
    if 0.5 <= v <= 2.0:
        return {"severity": "CONFIRM",
                "reasoning": f"ATR% {v:.2f}% — low-vol regime. Stable behavior; risk-adjusted outperformer (Frazzini-Pedersen)."}
    return None
