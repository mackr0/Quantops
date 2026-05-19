"""CONFIRM LONG when in a strong uptrend (ADX ≥ 25) AND pulled
back to RSI 40-50 (textbook "buy the dip" in trend)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "strong_uptrend_pullback"
DESCRIPTION = "CONFIRM LONG on pullback (RSI 40-50) in strong uptrend (ADX≥25)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    rsi = candidate.get("rsi")
    adx = candidate.get("adx")
    roc = candidate.get("roc_10")
    if rsi is None or adx is None or roc is None:
        return None
    try:
        r = float(rsi); a = float(adx); rc = float(roc)
    except (TypeError, ValueError):
        return None
    # Pullback (RSI 40-50) + strong trend (ADX ≥ 25) + 10d ROC still
    # positive (trend intact, this is a dip not a reversal)
    if 40 <= r <= 50 and a >= 25 and rc > 0:
        return {"severity": "CONFIRM",
                "reasoning": f"Pullback (RSI {r:.0f}) in strong trend (ADX {a:.0f}, ROC10 {rc:+.1f}%). Textbook buy-the-dip setup."}
    return None
