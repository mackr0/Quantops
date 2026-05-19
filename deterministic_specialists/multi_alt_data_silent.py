"""CAUTION on entries when alt-data dict is essentially empty.

When NONE of the alt-data sources have anything to say about a
name (no insider activity, no analyst revisions, no news, no
options flow), the candidate is operating on technicals alone.
Pure-technical entries have higher noise — the AI should weight
the technical signal more conservatively.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "multi_alt_data_silent"
DESCRIPTION = "CAUTION when alt-data is silent across all sources (pure-technical entry)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")
_SIGNAL_KEYS = (
    "insider", "insider_cluster", "analyst_estimates", "options",
    "short", "dark_pool", "earnings_surprise", "stocktwits_sentiment",
    "patent_activity", "transcript_sentiment", "congressional_recent",
    "activist_13dg", "recent_8k_events",
)


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    if not alt:
        # ablation profiles (NoAltData) — don't fire on a designed-off pipeline
        return None
    signal_count = 0
    for key in _SIGNAL_KEYS:
        v = alt.get(key)
        if isinstance(v, dict) and v and len(v) > 1:
            signal_count += 1
    if signal_count >= 2:
        return None
    return {"severity": "CAUTION",
            "reasoning": f"Only {signal_count} of {len(_SIGNAL_KEYS)} alt-data sources carry signal — pure-technical entry; weigh chart conservatively."}
