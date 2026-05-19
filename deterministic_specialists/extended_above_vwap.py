"""CAUTION LONG when price is extended >3% above session VWAP.

Intraday algos use VWAP as their fair-value anchor. Entering >3%
above VWAP means buying at a price the algorithmic flow has
already determined is rich. Pullback to VWAP is the typical
intraday pattern.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "extended_above_vwap"
DESCRIPTION = "CAUTION LONG when price > 3% above session VWAP"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    vwap_dist = candidate.get("pct_from_vwap")
    if vwap_dist is None:
        return None
    try:
        d = float(vwap_dist)
    except (TypeError, ValueError):
        return None
    if d > 3.0:
        return {
            "severity": "CAUTION",
            "reasoning": (
                f"Price is +{d:.1f}% extended above session VWAP. "
                "Algo flow treats VWAP as fair value; intraday "
                "pullback to VWAP is the base case."
            ),
        }
    return None
