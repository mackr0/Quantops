"""CONFIRM LONG when app-store ranking has jumped meaningfully
(consumer-product adoption proxy)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "app_store_ranking_jump"
DESCRIPTION = "CONFIRM LONG on app-store ranking jump"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    asr = alt.get("app_store_ranking") or {}
    delta = asr.get("rank_delta_wow") or asr.get("delta_wow")
    if delta is None:
        return None
    try:
        d = int(delta)
    except (TypeError, ValueError):
        return None
    # Rank delta is negative when ranking IMPROVES (rank 50 → rank 20 = -30)
    if d <= -10:
        return {"severity": "CONFIRM",
                "reasoning": f"App-store rank improved by {abs(d)} positions WoW. Consumer-product adoption accelerating."}
    return None
