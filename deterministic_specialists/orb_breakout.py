"""CONFIRM signal on opening-range breakout (ORB).

ORB (price clears the first-30-min high/low) is one of the
oldest documented intraday edge patterns. Combined with a
directional signal it's a classic momentum entry — the move
is happening NOW.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "orb_breakout"
DESCRIPTION = "CONFIRM directional signal on opening-range breakout"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    intra = alt.get("intraday") or {}
    if not intra.get("opening_range_breakout"):
        return None
    return {
        "severity": "CONFIRM",
        "reasoning": (
            "Opening-range breakout in progress. Classic documented "
            "intraday momentum edge; the move is happening now."
        ),
    }
