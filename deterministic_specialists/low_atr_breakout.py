"""CAUTION on breakout signals when ATR has been very compressed.

A breakout out of a tight (low-ATR) range OFTEN works — that's the
"squeeze + release" pattern. But it also often fails because the
move is too narrow to give the price action room to confirm, and
stops are typically too tight. Combine with the breakout-specific
reason text to limit firing to the actual claim.

We mark this as CAUTION not VETO because the squeeze pattern
genuinely does work plenty of times — the AI should weigh
position sizing carefully when this fires.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


NAME = "low_atr_breakout"
DESCRIPTION = "CAUTION on breakout when ATR < 1% of price (very compressed range)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")

_ATR_PCT_THRESHOLD = 1.0
_BREAKOUT_KEYWORDS = ("breakout", "break out", "broke out", "squeeze")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    reason = (candidate.get("reason") or "").lower()
    if not any(kw in reason for kw in _BREAKOUT_KEYWORDS):
        return None
    atr_pct = candidate.get("atr_pct")
    if atr_pct is None:
        return None
    try:
        a = float(atr_pct)
    except (TypeError, ValueError):
        return None
    if a < _ATR_PCT_THRESHOLD:
        return {
            "severity": "CAUTION",
            "reasoning": (
                f"Breakout fires from a low-ATR ({a:.2f}%) range. "
                "Tight ranges can release into clean moves but also "
                "stop out quickly — size down or use a wider stop."
            ),
        }
    return None
