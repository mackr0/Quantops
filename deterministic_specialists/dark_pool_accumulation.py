"""CONFIRM LONG when there is meaningful dark-pool ATS volume.

Dark-pool prints reflect institutional accumulation/distribution
done off-exchange. Heavy dark-pool volume relative to lit volume
suggests big buyers/sellers operating below the radar. We can't
tell direction from print volume alone, but the sheer presence
of institutional flow is a quality marker.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "dark_pool_accumulation"
DESCRIPTION = "CONFIRM signal when meaningful dark-pool ATS volume present"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    dp = alt.get("dark_pool") or {}
    vol = dp.get("ats_volume", 0) or 0
    venues = dp.get("num_venues", 0) or 0
    if vol < 100_000:
        return None
    return {
        "severity": "CONFIRM",
        "reasoning": (
            f"Dark-pool volume {vol:,} shares across {venues} ATS venues. "
            "Institutional flow operating off-exchange — quality marker "
            "regardless of direction."
        ),
    }
