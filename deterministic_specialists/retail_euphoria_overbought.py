"""VETO LONG when RSI > 75 AND retail sentiment is extremely bullish.

Mirror of `retail_panic_oversold`. Crowd euphoria + price extension
is one of the most-faded combinations.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "retail_euphoria_overbought"
DESCRIPTION = "VETO LONG on RSI>75 + retail sentiment very bullish"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    rsi = candidate.get("rsi")
    if rsi is None:
        return None
    try:
        r = float(rsi)
    except (TypeError, ValueError):
        return None
    if r <= 75:
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
    if nv < 0.5:
        return None
    return {"severity": "VETO",
            "reasoning": f"RSI {r:.0f} overbought + StockTwits sentiment {nv:+.2f}. Crowd euphoria at price extension — heavily faded combination."}
