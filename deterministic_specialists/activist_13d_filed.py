"""CONFIRM LONG when a recent Schedule 13D was filed.

13D filings (vs 13G) indicate an activist holder with intent to
influence management. Activist arrivals historically produce
~10% outperformance over the following 12 months (Brav, Jiang,
Partnoy, Thomas 2008). Strong signal regardless of chart.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "activist_13d_filed"
DESCRIPTION = "CONFIRM LONG on recent Schedule 13D filing (activist arrival)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    activist = alt.get("activist_13dg") or {}
    if not activist.get("has_13d"):
        return None
    return {
        "severity": "CONFIRM",
        "reasoning": (
            "Recent Schedule 13D filing — activist holder with intent "
            "to influence. Documented ~10% outperformance over 12mo."
        ),
    }
