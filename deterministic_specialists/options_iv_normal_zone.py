"""CONFIRM directional bet when options IV rank is in normal zone
(25-60) — premium isn't pricing extreme moves either direction."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "options_iv_normal_zone"
DESCRIPTION = "CONFIRM directional bet when IV rank in normal zone (25-60)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    opts = alt.get("options") or {}
    iv = opts.get("iv_rank")
    if iv is None:
        return None
    try:
        v = float(iv)
    except (TypeError, ValueError):
        return None
    if 25 <= v < 60:
        return {"severity": "CONFIRM",
                "reasoning": f"Options IV rank {v:.0f} — normal zone. Premium isn't pricing extremes; directional bet has cleaner expected payoff."}
    return None
