"""CONFIRM LONG when RSI < 30 AND retail sentiment is bearish.

Retail capitulation at oversold is the textbook contrarian setup.
The crowd's pessimism + price exhaustion combine into one of the
highest-base-rate reversal patterns documented.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "retail_panic_oversold"
DESCRIPTION = "CONFIRM LONG on RSI<30 + retail sentiment bearish"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    rsi = candidate.get("rsi")
    if rsi is None:
        return None
    try:
        r = float(rsi)
    except (TypeError, ValueError):
        return None
    if r >= 30:
        return None
    alt = candidate.get("alt_data") or {}
    s = alt.get("stocktwits_sentiment") or {}
    ns = s.get("net_sentiment_7d")
    if ns is None:
        return None
    try:
        nv = float(ns)
    except (TypeError, ValueError):
        return None
    if nv >= -0.30:
        return None
    return {"severity": "CONFIRM",
            "reasoning": f"RSI {r:.0f} oversold + StockTwits sentiment {nv:+.2f}. Retail capitulation at price exhaustion — textbook reversal setup."}
