"""CAUTION LONG when retail sentiment (StockTwits) is extreme
bullish.

Retail sentiment extremes are contrarian indicators. When
StockTwits 7d net sentiment is >+0.7 (essentially all-bull retail
chatter), the marginal buyer has already arrived. Doesn't fire
on moderate bullish (+0.3 to +0.5 is healthy).
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "stocktwits_extreme_bullish"
DESCRIPTION = "CAUTION LONG when StockTwits 7d sentiment > +0.70 (retail euphoria)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    s = alt.get("stocktwits_sentiment") or {}
    ns = s.get("net_sentiment_7d")
    if ns is None:
        return None
    try:
        v = float(ns)
    except (TypeError, ValueError):
        return None
    if v < 0.70:
        return None
    return {
        "severity": "CAUTION",
        "reasoning": (
            f"StockTwits 7d sentiment {v:+.2f} — retail euphoria. "
            "Marginal buyer has already arrived."
        ),
    }
