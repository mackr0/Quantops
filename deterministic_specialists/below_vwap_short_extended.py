"""CAUTION SHORT when price is extended >3% below session VWAP.

Mirror of `extended_above_vwap`. Shorting >3% below VWAP means
entering after the algo flow has already done the work — the
bounce-to-VWAP is the typical pattern.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "below_vwap_short_extended"
DESCRIPTION = "CAUTION SHORT when price > 3% below session VWAP"
APPLIES_TO_SIGNALS = ("SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    vwap_dist = candidate.get("pct_from_vwap")
    if vwap_dist is None:
        return None
    try:
        d = float(vwap_dist)
    except (TypeError, ValueError):
        return None
    if d < -3.0:
        return {
            "severity": "CAUTION",
            "reasoning": (
                f"Price is {d:.1f}% extended below session VWAP. "
                "Bounce-to-VWAP is the typical intraday pattern; "
                "shorting here often catches the wick low."
            ),
        }
    return None
