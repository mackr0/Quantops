"""CAUTION LONG when app-store ranking dropped meaningfully."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "app_store_ranking_drop"
DESCRIPTION = "CAUTION LONG on app-store ranking drop"
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
    if d >= 10:
        return {"severity": "CAUTION",
                "reasoning": f"App-store rank fell {d} positions WoW. Consumer engagement weakening."}
    return None
