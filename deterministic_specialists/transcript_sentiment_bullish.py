"""CONFIRM LONG on bullish earnings-call transcript sentiment."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "transcript_sentiment_bullish"
DESCRIPTION = "CONFIRM LONG on bullish transcript tone"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    t = alt.get("transcript_sentiment") or {}
    if not t.get("has_data"):
        return None
    tone = (t.get("tone") or "").lower()
    if tone not in ("bullish", "positive", "confident"):
        return None
    phrases = ", ".join((t.get("key_phrases") or [])[:2])
    detail = f" — {phrases}" if phrases else ""
    return {"severity": "CONFIRM",
            "reasoning": f"Earnings-call transcript tone {tone}{detail}."}
