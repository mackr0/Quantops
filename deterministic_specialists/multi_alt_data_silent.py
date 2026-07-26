"""CAUTION on entries when alt-data is truly silent about the name.

When NONE of the alt-data sources have anything to say (no insider
activity, no analyst coverage, no options flow, no dark-pool prints,
no retail chatter...), the candidate is operating on technicals
alone. Pure-technical entries have higher noise — the AI should
weight the technical signal more conservatively.

P1.4 (2026-07-26): "carries signal" is now a per-source predicate on
the real payload shapes. The old heuristic (`len(dict) > 1`) counted
EVERY source as a carrier because every reader returns a multi-key
dict even when it has nothing to say (`{"is_cluster": false, ...}`,
`{"ats_volume": 0, ...}`) — the specialist could never fire.
patent_activity dropped (source disabled upstream — PatentsView v1
API deprecated, never in the bundle).
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "multi_alt_data_silent"
DESCRIPTION = "CAUTION when alt-data is silent across all sources (pure-technical entry)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")

# Per-source "does this payload actually say something about the
# name" predicates, keyed by the alt_data bundle key and matched to
# each reader's real return shape.
_SIGNAL_TESTS = {
    "insider": lambda v: ((v.get("recent_buys") or 0)
                          + (v.get("recent_sells") or 0)) > 0,
    "insider_cluster": lambda v: bool(v.get("is_cluster")),
    "analyst_estimates": lambda v: v.get("eps_current_estimate") is not None,
    "options": lambda v: bool(v.get("has_options"))
                         and ((v.get("total_call_volume") or 0)
                              + (v.get("total_put_volume") or 0)) > 0,
    "short": lambda v: v.get("short_pct_float") is not None,
    "dark_pool": lambda v: (v.get("ats_volume") or 0) > 0,
    "earnings_surprise": lambda v: (v.get("total_quarters") or 0) > 0,
    "stocktwits_sentiment": lambda v: bool(v.get("covered"))
                                      and (v.get("message_count_7d") or 0) > 0,
    "transcript_sentiment": lambda v: bool(v.get("has_data")),
    "congressional_recent": lambda v: (v.get("trades_60d") or 0) > 0,
    "activist_13dg": lambda v: (v.get("count") or 0) > 0,
    "recent_8k_events": lambda v: (v.get("count") or 0) > 0,
}


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    if not alt:
        # ablation profiles (NoAltData) — don't fire on a designed-off pipeline
        return None
    signal_count = 0
    for key, carries_signal in _SIGNAL_TESTS.items():
        v = alt.get(key)
        if not isinstance(v, dict) or not v:
            continue
        try:
            if carries_signal(v):
                signal_count += 1
        except (TypeError, ValueError):
            continue
    # Narrowed 2026-05-18 PM (post-Phase-3 audit). Original "fire when
    # <2 sources carry signal" fired on most small-cap candidates and
    # biased the panel against entries that had nothing wrong with
    # them — pure-technical entries on small-caps are routine.
    # Requires ZERO alt-data signal carriers to fire — truly silent.
    if signal_count >= 1:
        return None
    return {"severity": "CAUTION",
            "reasoning": f"All {len(_SIGNAL_TESTS)} alt-data sources are silent — pure-technical entry with no confirming flow signals."}
