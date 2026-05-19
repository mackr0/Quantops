"""VETO LONG when a breakout signal fires but volume hasn't
confirmed.

Breakouts on light volume are the most-faded pattern in technical
analysis. Without the buying surge, the move is typically driven
by a few large sellers stepping aside rather than genuine demand,
and reversal back into the prior range is the base case.

Fires only when the candidate's `reason` text indicates a breakout
or momentum signal (so we don't VETO every weak-volume BUY — only
the ones that EXPLICITLY claim a breakout).
"""
from __future__ import annotations

from typing import Any, Dict, Optional


NAME = "volume_dry_breakout"
DESCRIPTION = "VETO LONG when 'breakout' in reason AND volume_ratio < 1.0×"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")

_VOLUME_THRESHOLD = 1.0  # at or below average
_BREAKOUT_KEYWORDS = ("breakout", "break out", "broke out", "momentum")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    reason = (candidate.get("reason") or "").lower()
    if not any(kw in reason for kw in _BREAKOUT_KEYWORDS):
        return None
    vol_ratio = candidate.get("volume_ratio")
    if vol_ratio is None:
        return None
    try:
        v = float(vol_ratio)
    except (TypeError, ValueError):
        return None
    if v < _VOLUME_THRESHOLD:
        return {
            "severity": "VETO",
            "reasoning": (
                f"Breakout/momentum signal fires but volume ratio "
                f"{v:.2f}× is below average. Unconfirmed breakouts "
                "are the most-faded pattern in TA."
            ),
        }
    return None
