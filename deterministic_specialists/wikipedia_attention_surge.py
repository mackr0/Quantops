"""CAUTION LONG on wikipedia pageview surge (attention proxy)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "wikipedia_attention_surge"
DESCRIPTION = "CAUTION LONG on wikipedia pageview surge"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    wp = alt.get("wikipedia_pageviews") or {}
    if not wp.get("has_surge") and not wp.get("is_spike"):
        return None
    pct = wp.get("surge_pct") or wp.get("change_pct") or 0
    return {"severity": "CAUTION",
            "reasoning": f"Wikipedia pageview surge (+{pct}%). Attention spikes often mark short-term tops."}
