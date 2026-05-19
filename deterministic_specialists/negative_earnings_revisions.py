"""CAUTION LONG when analyst EPS estimates are being revised DOWN.

Inverse of `positive_earnings_revisions`. Down-revisions cluster
ahead of disappointing quarters. Going LONG against a wave of
analyst downgrades requires a specific contrarian thesis.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "negative_earnings_revisions"
DESCRIPTION = "CAUTION LONG when EPS revision direction is DOWN"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    est = alt.get("analyst_estimates") or {}
    direction = (est.get("eps_revision_direction") or "").lower()
    if direction not in ("down", "lower", "bearish"):
        return None
    magnitude = est.get("revision_magnitude_pct")
    mag_str = ""
    try:
        if magnitude is not None:
            mag_str = f" by {abs(float(magnitude)):.1f}%"
    except (TypeError, ValueError):
        pass
    return {
        "severity": "CAUTION",
        "reasoning": (
            f"Analyst EPS revisions {direction.upper()}{mag_str}. "
            "Down-revisions cluster ahead of disappointing quarters."
        ),
    }
