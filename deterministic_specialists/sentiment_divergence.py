"""CAUTION LONG when price is rising (positive ROC) but retail
sentiment is bearish — divergence often precedes reversal."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "sentiment_divergence"
DESCRIPTION = "CAUTION LONG when price rising but retail sentiment bearish"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    roc = candidate.get("roc_10")
    if roc is None:
        return None
    try:
        r = float(roc)
    except (TypeError, ValueError):
        return None
    if r <= 2:
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
    if nv > -0.20:
        return None
    return {"severity": "CAUTION",
            "reasoning": f"Price up (ROC10 {r:+.1f}%) but retail sentiment {nv:+.2f} bearish — divergence often precedes reversal."}
