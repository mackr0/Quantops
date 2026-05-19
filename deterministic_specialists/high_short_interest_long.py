"""CAUTION LONG when short interest is unusually high.

Two interpretations, both worth surfacing:
  - Bearish: smart money has structurally positioned against this
    name, betting on a thesis breakdown.
  - Bullish: extreme short interest creates squeeze fuel — but the
    AI should weigh whether a squeeze catalyst actually exists, not
    just buy on high SI alone.

Either way the operator wants to know. CAUTION is the right
severity — high SI is asymmetric information, not a stop sign.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


NAME = "high_short_interest_long"
DESCRIPTION = "CAUTION LONG when short interest > 20% of float"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")

_SI_THRESHOLD = 20.0


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    short = alt.get("short") or {}
    si = short.get("short_pct_float")
    if si is None:
        return None
    try:
        si_f = float(si)
    except (TypeError, ValueError):
        return None
    if si_f >= _SI_THRESHOLD:
        squeeze_risk = short.get("squeeze_risk", "unknown")
        return {
            "severity": "CAUTION",
            "reasoning": (
                f"Short interest is {si_f:.1f}% of float — heavy "
                f"institutional short positioning (squeeze risk: {squeeze_risk}). "
                "Confirm there's a catalyst before assuming a squeeze fires."
            ),
        }
    return None
