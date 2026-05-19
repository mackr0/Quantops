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
    # Narrowed 2026-05-18 PM (post-Phase-3 audit). Original |CMF| ≤
    # 0.05 fired on most range-bound names every day, creating
    # CAUTION wallpaper that biased the LLM against routine entries.
    # Now requires near-zero CMF (|v| ≤ 0.02) to fire — true neutrality.
    if abs(v) <= 0.02:
        return {"severity": "CAUTION",
                "reasoning": f"CMF {v:+.2f} — flat. Neither buyers nor sellers committed; directional flow conviction is absent."}
    return None
