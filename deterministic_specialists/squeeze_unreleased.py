"""CAUTION on directional signals when TTM-squeeze IS firing but
volume is still dry — the squeeze hasn't released yet.

A fired squeeze with no volume surge is "compression noise" — the
band crossing happened but flow hasn't picked a direction. The
edge is in the release WITH volume; entering before that often
results in chop.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "squeeze_unreleased"
DESCRIPTION = "CAUTION when squeeze fires but volume_ratio < 1.2 (no release yet)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    if not candidate.get("squeeze"):
        return None
    vr = candidate.get("volume_ratio")
    if vr is None:
        return None
    try:
        v = float(vr)
    except (TypeError, ValueError):
        return None
    if v < 1.2:
        return {"severity": "CAUTION",
                "reasoning": f"Squeeze fired but volume {v:.1f}× still dry — release hasn't picked a direction yet."}
    return None
