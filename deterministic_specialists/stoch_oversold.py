"""CONFIRM LONG when Stochastic RSI < 20 AND price is in an uptrend
(ROC positive). Mean-reversion with the trend."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "stoch_oversold"
DESCRIPTION = "CONFIRM LONG on StochRSI < 20 in an uptrend"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    s = candidate.get("stoch_rsi")
    roc = candidate.get("roc_10")
    if s is None or roc is None:
        return None
    try:
        sv = float(s); rv = float(roc)
    except (TypeError, ValueError):
        return None
    if sv <= 20 and rv > 0:
        return {"severity": "CONFIRM",
                "reasoning": f"StochRSI {sv:.0f} ≤ 20 in uptrend (ROC10 {rv:+.1f}%) — mean-revert with trend."}
    return None
