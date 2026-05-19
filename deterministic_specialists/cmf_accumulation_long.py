"""CONFIRM LONG when Chaikin Money Flow shows accumulation.

CMF > +0.10 over 20 days indicates net buying pressure — flow
confirming the LONG thesis. Positive corollary to
`cmf_distribution_long`.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "cmf_accumulation_long"
DESCRIPTION = "CONFIRM LONG when CMF > +0.10 (institutional accumulation)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    cmf = candidate.get("cmf")
    if cmf is None:
        return None
    try:
        v = float(cmf)
    except (TypeError, ValueError):
        return None
    if v <= 0.10:
        return None
    return {
        "severity": "CONFIRM",
        "reasoning": (
            f"CMF {v:+.2f} (>+0.10) — institutional accumulation. "
            "Flow confirming the LONG thesis."
        ),
    }
