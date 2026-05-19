"""CAUTION on either-side signal when ADX < 15 (no trend at all).

ADX between 0-15 is the "no man's land" regime — neither trend nor
clean reversal works well. Edge compresses; better to wait for
direction to develop. Distinct from `weak_adx_breakout` which only
fires when an actual breakout signal is making the claim.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "low_adx_no_trade"
DESCRIPTION = "CAUTION on directional signal when ADX < 15 (no trend regime)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    adx = candidate.get("adx")
    if adx is None:
        return None
    try:
        a = float(adx)
    except (TypeError, ValueError):
        return None
    # Tightened 2026-05-18 PM (post-Phase-3 audit). Original ADX < 15
    # fired on most range-bound names; the panel got noisy with
    # CAUTIONs for routine non-trending entries. Now requires < 12
    # for true "no man's land" trend absence.
    if a < 12:
        return {"severity": "CAUTION",
                "reasoning": f"ADX {a:.0f} — no-trend regime. Directional edge compresses; size down."}
    return None
