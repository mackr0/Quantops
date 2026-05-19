"""CAUTION when CMF is in the neutral zone (-0.05 to +0.05) AND
the entry needs flow confirmation. Money flow neutrality means
neither buyers nor sellers are committed."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "cmf_neutral_low_signal"
DESCRIPTION = "CAUTION when CMF in neutral zone (no flow conviction)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    cmf = candidate.get("cmf")
    if cmf is None:
        return None
    try:
        v = float(cmf)
    except (TypeError, ValueError):
        return None
    if abs(v) <= 0.05:
        return {"severity": "CAUTION",
                "reasoning": f"CMF {v:+.2f} — neutral. Neither buyers nor sellers committed; directional flow conviction is absent."}
    return None
