"""CAUTION LONG when market breadth is washed out.

Fewer than 30% of the universe closing above its own 20-day mean is a
tape where most longs are fighting the market. Computed from the
screener's own bars (market_breadth) — no external source.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "breadth_washout_long_caution"
DESCRIPTION = "CAUTION LONG when <30% of the universe is above its 20-day mean"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    macro = (candidate.get("alt_data") or {}).get("macro") or {}
    b = macro.get("breadth") or {}
    if not b.get("has_data") or b.get("regime") != "washout":
        return None
    return {
        "severity": "CAUTION",
        "reasoning": (f"Breadth washed out: {b.get('pct_above_sma20', 0):.0f}% of {b.get('n', 0)} "
                      f"symbols above their 20-day mean, advancers/decliners "
                      f"{b.get('adv_dec', '?')} — most longs are fighting the tape."),
    }
