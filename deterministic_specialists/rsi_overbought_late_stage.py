"""VETO LONG when RSI is extreme AND the stock is at/near 52-week
high without a recent pullback.

Classic late-stage trap: momentum chasers buy a leader at the top.
The pattern combines two known failure modes — overbought RSI on
its own is a weak signal (strong trends stay overbought for weeks),
but PAIRED with proximity to the 52-week high and the absence of
a recent breather, the combination has historically been a high-
probability fade setup.

Rule fires only on BUY/STRONG_BUY signals. The corollary for SHORT
signals (extreme oversold near 52-week low) is a separate rule
because the asymmetry of long vs short risk profiles is different.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


NAME = "rsi_overbought_late_stage"
DESCRIPTION = "VETO LONG when RSI>80 AND price within 2% of 52-week high"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")

_RSI_THRESHOLD = 80
_PROXIMITY_PCT = 2.0  # within 2% of 52w high


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    rsi = candidate.get("rsi")
    pct_from_52h = candidate.get("pct_from_52w_high")
    if rsi is None or pct_from_52h is None:
        return None
    try:
        rsi_f = float(rsi)
        pct_f = float(pct_from_52h)
    except (TypeError, ValueError):
        return None
    # pct_from_52w_high is reported as a negative-or-zero number when
    # the stock is below its 52w high (e.g., -1.5 = 1.5% below). The
    # absolute distance must be small.
    distance = abs(pct_f)
    if rsi_f >= _RSI_THRESHOLD and distance <= _PROXIMITY_PCT:
        return {
            "severity": "VETO",
            "reasoning": (
                f"RSI {rsi_f:.0f} ≥ {_RSI_THRESHOLD} and price is "
                f"{distance:.1f}% from 52-week high — late-stage trap "
                "pattern. Momentum chasing the leader at the top."
            ),
        }
    return None
