"""CAUTION when CBOE SKEW index signals extreme tail-risk pricing.

SKEW (the "black swan" gauge) measures OTM-put premium vs ATM.
High SKEW means options market is paying up for downside
protection — institutional players see tail risk. Doesn't time
crashes but argues for defensive sizing.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "cboe_skew_extreme"
DESCRIPTION = "CAUTION when CBOE SKEW signals elevated tail-risk pricing"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    macro = alt.get("macro") or {}
    skew = macro.get("cboe_skew") or {}
    signal = (skew.get("skew_signal") or "").lower()
    if signal not in ("high", "extreme", "very_high"):
        return None
    return {
        "severity": "CAUTION",
        "reasoning": (
            f"CBOE SKEW {signal} — institutional flows paying up for "
            "tail-risk protection. Defensive sizing argued."
        ),
    }
