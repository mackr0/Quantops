"""CONFIRM LONG when analyst EPS estimates are being revised UP.

Earnings revision momentum is one of the most-cited factor signals
in equity research (Givoly & Lakonishok 1979; modern factor models
include "revision" as a distinct exposure). Up-revisions cluster
ahead of beat-and-raise quarters.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "positive_earnings_revisions"
DESCRIPTION = "CONFIRM LONG when EPS revision direction is UP/higher"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    est = alt.get("analyst_estimates") or {}
    direction = (est.get("eps_revision_direction") or "").lower()
    magnitude = est.get("revision_magnitude_pct")
    if direction not in ("up", "higher", "bullish"):
        return None
    mag_str = ""
    try:
        if magnitude is not None:
            mag_str = f" by {abs(float(magnitude)):.1f}%"
    except (TypeError, ValueError):
        pass
    return {
        "severity": "CONFIRM",
        "reasoning": (
            f"Analyst EPS revisions {direction.upper()}{mag_str}. "
            "Revision momentum clusters ahead of beat-and-raise quarters."
        ),
    }
