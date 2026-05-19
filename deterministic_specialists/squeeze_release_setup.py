"""CONFIRM signal when the squeeze indicator fires (Bollinger
inside Keltner = compression release).

The TTM-squeeze pattern: when Bollinger Bands contract inside
Keltner Channels then release, the resulting expansion tends to
have strong follow-through. Combined with a directional signal,
this is a known high-conviction setup.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "squeeze_release_setup"
DESCRIPTION = "CONFIRM directional signal when TTM-squeeze indicator fires"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    squeeze = candidate.get("squeeze")
    if not squeeze:
        return None
    return {
        "severity": "CONFIRM",
        "reasoning": (
            "TTM-squeeze fired (Bollinger inside Keltner). Compression-"
            "release expansion has strong follow-through with a "
            "directional signal."
        ),
    }
