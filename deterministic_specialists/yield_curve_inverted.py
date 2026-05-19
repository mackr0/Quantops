"""CAUTION sizing when yield curve is inverted.

Inverted yield curve (2s/10s) has historically preceded recessions
within 12-24 months. Doesn't kill all longs but argues for
defensive sizing and shorter holds.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "yield_curve_inverted"
DESCRIPTION = "CAUTION LONG sizing when yield curve is inverted"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    macro = alt.get("macro") or {}
    yc = macro.get("yield_curve") or {}
    signal = (yc.get("curve_signal") or "").lower()
    if signal not in ("inverted", "deeply_inverted"):
        return None
    return {
        "severity": "CAUTION",
        "reasoning": (
            f"Yield curve {signal} — historical recession lead "
            "indicator. Defensive sizing + shorter holds."
        ),
    }
