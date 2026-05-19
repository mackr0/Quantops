"""CAUTION when IV rank is at extreme highs.

High IV rank means options are pricing big expected moves —
typically due to upcoming catalyst (earnings, FDA, M&A rumor).
A directional LONG bet pays premium plus eats theta if the
catalyst doesn't deliver the size move priced in.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "options_iv_extreme_high"
DESCRIPTION = "CAUTION when IV rank ≥ 75 (extreme premium / catalyst priced in)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


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
    if v < 75:
        return None
    return {
        "severity": "CAUTION",
        "reasoning": (
            f"Options IV rank {v:.0f} — extreme. Market pricing in a "
            "big catalyst-driven move; directional bet pays the premium."
        ),
    }
