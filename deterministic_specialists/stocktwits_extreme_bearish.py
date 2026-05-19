"""CONFIRM LONG when retail sentiment (StockTwits) is extreme
bearish (contrarian buy signal).

Mirror of `stocktwits_extreme_bullish`. Extreme bearish retail
chatter (<-0.5) often marks capitulation lows. The reverse for
SHORT is encoded as a separate rule.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "stocktwits_extreme_bearish"
DESCRIPTION = "CONFIRM LONG when StockTwits 7d sentiment < -0.50 (capitulation)"
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
    if v > -0.50:
        return None
    return {
        "severity": "CONFIRM",
        "reasoning": (
            f"StockTwits 7d sentiment {v:+.2f} — retail capitulation. "
            "Contrarian buy signal historically marks lows."
        ),
    }
