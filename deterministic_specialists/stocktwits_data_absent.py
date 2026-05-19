"""CAUTION on small/illiquid LONG when StockTwits sentiment is
absent — no retail attention means no near-term liquidity catalyst."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "stocktwits_data_absent"
DESCRIPTION = "CAUTION LONG on small-cap when StockTwits chatter is absent"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    s = alt.get("stocktwits_sentiment") or {}
    # If alt_data is genuinely turned off the dict will be empty — skip.
    if not alt:
        return None
    if s.get("has_data") or s.get("net_sentiment_7d") is not None:
        return None
    p = candidate.get("price")
    if p is None:
        return None
    try:
        price = float(p)
    except (TypeError, ValueError):
        return None
    # Only fire on lower-priced names (small-cap proxy)
    if price > 30:
        return None
    return {"severity": "CAUTION",
            "reasoning": "Small-cap LONG with NO StockTwits chatter. No retail attention = no near-term liquidity catalyst."}
