"""CAUTION LONG when price is at a new high but momentum indicators
suggest the move is losing steam.

Approximation: we don't have multi-period RSI history in the
candidate dict, but we DO have the StochRSI (`stoch_rsi`) and the
classic RSI side by side. When RSI is near peak (>=70) but StochRSI
has already rolled over (<=50), the short-term momentum tail is
fading while the longer-term gauge still reads strong — the
textbook bearish-divergence shape.

This is a CAUTION, not a VETO: divergences can persist for
weeks. The AI should weigh it, not treat it as a hard stop.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


NAME = "bearish_divergence"
DESCRIPTION = "CAUTION LONG when RSI ≥ 70 but StochRSI ≤ 50 (momentum tail rolling over)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")

_RSI_FLOOR = 70
_STOCH_CEILING = 50


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    rsi = candidate.get("rsi")
    stoch = candidate.get("stoch_rsi")
    if rsi is None or stoch is None:
        return None
    try:
        rsi_f = float(rsi)
        stoch_f = float(stoch)
    except (TypeError, ValueError):
        return None
    if rsi_f >= _RSI_FLOOR and stoch_f <= _STOCH_CEILING:
        return {
            "severity": "CAUTION",
            "reasoning": (
                f"RSI {rsi_f:.0f} still elevated but StochRSI {stoch_f:.0f} "
                "has rolled over — short-term momentum fading while the "
                "longer gauge holds. Classic bearish-divergence shape."
            ),
        }
    return None
