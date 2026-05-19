"""CAUTION LONG when the candidate gapped up >2% AND is now near
the 52-week high.

Gap-into-resistance is the classic "supply zone" failure: longs who
bought at the prior high are finally back to breakeven and use the
gap as their exit, capping the move. The opening gap is usually
filled within a few days.

Fires on BUY signals only — the equivalent gap-down-into-support
pattern for shorts is its own rule.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


NAME = "gap_into_resistance"
DESCRIPTION = "CAUTION LONG when gap up >2% AND near 52-week high"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")

_GAP_THRESHOLD = 2.0
_PROXIMITY_PCT = 3.0  # within 3% of 52w high


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    gap = candidate.get("gap_pct")
    pct_from_52h = candidate.get("pct_from_52w_high")
    if gap is None or pct_from_52h is None:
        return None
    try:
        gap_f = float(gap)
        pct_f = float(pct_from_52h)
    except (TypeError, ValueError):
        return None
    if gap_f >= _GAP_THRESHOLD and abs(pct_f) <= _PROXIMITY_PCT:
        return {
            "severity": "CAUTION",
            "reasoning": (
                f"Gap +{gap_f:.1f}% into the 52-week high zone "
                f"({abs(pct_f):.1f}% from prior peak). Trapped longs "
                "from the prior top often use this gap as their exit."
            ),
        }
    return None
