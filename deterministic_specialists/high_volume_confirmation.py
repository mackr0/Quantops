"""CONFIRM signal when volume ≥ 3× the 20-day average.

3× volume on a directional move is institutional participation.
Both BUY and SHORT signals get higher follow-through with this
much volume behind them. The positive corollary to
`volume_dry_breakout`.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "high_volume_confirmation"
DESCRIPTION = "CONFIRM signal when volume_ratio ≥ 3× (institutional surge)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")
_THRESHOLD = 3.0


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    vr = candidate.get("volume_ratio")
    if vr is None:
        return None
    try:
        v = float(vr)
    except (TypeError, ValueError):
        return None
    if v >= _THRESHOLD:
        return {
            "severity": "CONFIRM",
            "reasoning": (
                f"Volume {v:.1f}× the 20-day average — institutional "
                "participation. Directional moves with this volume have "
                "higher follow-through."
            ),
        }
    return None
