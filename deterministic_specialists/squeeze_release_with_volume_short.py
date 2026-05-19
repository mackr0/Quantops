"""CONFIRM SHORT when squeeze fires WITH volume surge AND
strengthening trend (mirror of squeeze_then_release_buy)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "squeeze_release_with_volume_short"
DESCRIPTION = "CONFIRM SHORT on squeeze release with volume + strong ADX"
APPLIES_TO_SIGNALS = ("SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


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
                "reasoning": f"Squeeze fired SHORT-side with volume {v:.1f}× + ADX {a:.0f} — textbook release into downtrend."}
    return None
