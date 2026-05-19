"""CAUTION LONG when there are recent FDA inspection citations.

FDA 483s / Warning Letters are precursors to enforcement action
(seizures, consent decrees, plant shutdowns). Biotech/pharma names
with recent citations face binary downside that doesn't show up in
chart structure.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "fda_inspection_warning"
DESCRIPTION = "CAUTION LONG when recent FDA inspection citations present"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    fda = alt.get("fda_inspections") or {}
    n = fda.get("recent_citations_count", 0) or 0
    if n <= 0:
        return None
    return {
        "severity": "CAUTION",
        "reasoning": (
            f"Recent FDA citations: {n}. 483s/Warning Letters are "
            "precursors to enforcement (plant shutdowns, consent decrees)."
        ),
    }
