"""CAUTION LONG when Chaikin Money Flow shows distribution.

CMF < -0.1 over 20 days indicates net selling pressure even when
price action looks neutral. Distribution under the surface
predicts breakdown — going LONG into it is fighting institutional
flow.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "cmf_distribution_long"
DESCRIPTION = "CAUTION LONG when CMF < -0.10 (institutional distribution)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    cmf = candidate.get("cmf")
    if cmf is None:
        return None
    try:
        v = float(cmf)
    except (TypeError, ValueError):
        return None
    if v >= -0.10:
        return None
    return {
        "severity": "CAUTION",
        "reasoning": (
            f"CMF {v:+.2f} (<-0.10) — institutional distribution under "
            "the surface. LONG fights the flow."
        ),
    }
