"""CONFIRM LONG when price is modestly above session VWAP.

Algo benchmarks treat VWAP as fair-value. Being modestly above
VWAP (0.1% to 2.0%) means the day's net flow is bullish without
the parabolic-extension warning of >3% above (which has its own
rule).
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "above_vwap_long_confirm"
DESCRIPTION = "CONFIRM LONG when price 0.1%-2.0% above session VWAP"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    v = candidate.get("pct_from_vwap")
    if v is None:
        return None
    try:
        d = float(v)
    except (TypeError, ValueError):
        return None
    if 0.1 <= d <= 2.0:
        return {"severity": "CONFIRM",
                "reasoning": f"Price +{d:.1f}% above session VWAP — net intraday flow bullish without extension."}
    return None
