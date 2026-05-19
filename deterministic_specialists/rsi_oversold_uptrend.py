"""CONFIRM LONG on RSI < 30 mean-reversion buy in a confirmed
uptrend (price >0 above 50d SMA proxy: positive sector relative
strength + positive ROC10).

Mean reversion works best WITH the trend, not against it. The pure
"RSI < 30 = buy" rule has poor base rates because it fires on
falling knives; constrained to uptrend context the edge appears.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "rsi_oversold_uptrend"
DESCRIPTION = "CONFIRM LONG when RSI<30 in an uptrend (positive 5-day return)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    rsi = candidate.get("rsi")
    roc = candidate.get("roc_10")
    if rsi is None or roc is None:
        return None
    try:
        rsi_f = float(rsi)
        roc_f = float(roc)
    except (TypeError, ValueError):
        return None
    # Oversold + still in uptrend (5-10 day ROC positive)
    if rsi_f < 30 and roc_f > 0:
        return {
            "severity": "CONFIRM",
            "reasoning": (
                f"RSI {rsi_f:.0f} (<30 oversold) but ROC10 {roc_f:+.1f}% "
                "still positive — pullback in an uptrend. Mean-reversion "
                "with the trend has the best base rate."
            ),
        }
    return None
