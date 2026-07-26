"""CAUTION when bid-ask spread is wide (proxy: slippage estimate
in the 15-30 bps band even for moderate sizes — indicates a thin
order book; 30+ bps escalates to slippage_high_caution instead).

P1.4 (2026-07-26): reads the structured `slippage_estimate` dict the
pipeline attaches (total_bps), falling back to parsing the prompt
string "exec cost ~X bps (...)". The original parsed a '%' pattern
the renderer stopped emitting — dead since the format change.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

from deterministic_specialists.slippage_high_caution import (
    _SLIP_THRESHOLD_BPS, slippage_bps,
)

NAME = "wide_spread_caution"
DESCRIPTION = "CAUTION when slippage estimate is 15-30 bps (wide-spread proxy)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")
_WIDE_SPREAD_BPS = 15.0


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    v = slippage_bps(candidate)
    if v is None:
        return None
    # Below the slippage_high_caution threshold but above 15 bps.
    if _WIDE_SPREAD_BPS <= v < _SLIP_THRESHOLD_BPS:
        return {"severity": "CAUTION",
                "reasoning": f"Spread/slippage proxy {v:.0f} bps — thin order book; bid-ask widens on size."}
    return None
