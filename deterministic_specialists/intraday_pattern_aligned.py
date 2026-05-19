"""CONFIRM signal when intraday pattern is positively aligned with
the signal direction (intraday momentum confirms the trade)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "intraday_pattern_aligned"
DESCRIPTION = "CONFIRM signal when intraday pattern aligns with direction"
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
    aligned = (
        (sig in _LONG_SIGS and any(p in pattern for p in _LONG_PATTERNS))
        or (sig in _SHORT_SIGS and any(p in pattern for p in _SHORT_PATTERNS))
    )
    if not aligned:
        return None
    return {"severity": "CONFIRM",
            "reasoning": f"Intraday pattern '{pattern}' aligns with signal direction."}
