"""CAUTION when intraday pattern OPPOSES the signal direction
(intraday flow contradicts the trade)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "intraday_pattern_opposed"
DESCRIPTION = "CAUTION when intraday pattern opposes signal direction"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")
_LONG_PATTERNS = ("bullish", "breakout", "above vwap", "upward")
_SHORT_PATTERNS = ("bearish", "breakdown", "below vwap", "downward")
_LONG_SIGS = {"BUY", "STRONG_BUY", "WEAK_BUY"}
_SHORT_SIGS = {"SELL", "STRONG_SELL", "WEAK_SELL", "SHORT"}


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    intra = alt.get("intraday") or {}
    pattern = (intra.get("pattern") or "").lower()
    if not pattern:
        return None
    sig = (candidate.get("signal") or "").upper()
    opposed = (
        (sig in _LONG_SIGS and any(p in pattern for p in _SHORT_PATTERNS))
        or (sig in _SHORT_SIGS and any(p in pattern for p in _LONG_PATTERNS))
    )
    if not opposed:
        return None
    return {"severity": "CAUTION",
            "reasoning": f"Intraday pattern '{pattern}' opposes the signal direction — intraday flow contradicts the trade thesis."}
