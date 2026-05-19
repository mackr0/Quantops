"""CONFIRM LONG when TTM-squeeze fired AND volume is now expanding
AND ADX is rising (the textbook squeeze-release momentum entry)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "squeeze_then_release_buy"
DESCRIPTION = "CONFIRM LONG when squeeze fires WITH volume surge AND strengthening trend"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    if not candidate.get("squeeze"):
        return None
    vr = candidate.get("volume_ratio")
    adx = candidate.get("adx")
    if vr is None or adx is None:
        return None
    try:
        v = float(vr); a = float(adx)
    except (TypeError, ValueError):
        return None
    if v >= 1.5 and a >= 20:
        return {"severity": "CONFIRM",
                "reasoning": f"Squeeze fired with volume {v:.1f}× + ADX {a:.0f} — textbook release into trend."}
    return None
