"""CAUTION on entries when earnings are within the profile's
configured avoidance window.

Earnings introduce 2-sigma overnight gap risk that disrupts
sizing and stops. The user's `avoid_earnings_days` setting
encodes their tolerance — this rule fires when a candidate
violates it, surfacing the conflict to the AI.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "earnings_within_window"
DESCRIPTION = "CAUTION when days_to_earnings within profile.avoid_earnings_days"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    if ctx is None:
        return None
    window = getattr(ctx, "avoid_earnings_days", None)
    if not window:
        return None
    alt = candidate.get("alt_data") or {}
    ie = alt.get("insider_earnings") or {}
    d2e = ie.get("days_to_earnings")
    if d2e is None:
        return None
    try:
        d = int(d2e)
        w = int(window)
    except (TypeError, ValueError):
        return None
    if 0 <= d <= w:
        return {
            "severity": "CAUTION",
            "reasoning": (
                f"Earnings in {d} day(s) — within profile's "
                f"{w}-day avoidance window. Overnight gap risk."
            ),
        }
    return None
