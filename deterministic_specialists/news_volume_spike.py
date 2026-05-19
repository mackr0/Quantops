"""CAUTION when 3+ news items hit and the system has no specific
catalyst interpretation.

A news cluster is signal — something happened. If our SEC-alert
pipeline didn't catch a specific item and the news count is high,
there's an information event we may not fully understand.
Defensive sizing is appropriate.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "news_volume_spike"
DESCRIPTION = "CAUTION on news cluster (3+ items) without parsed SEC catalyst"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    news = candidate.get("news") or []
    if len(news) < 3:
        return None
    # If SEC pipeline already parsed a specific high-severity item,
    # the AI already gets that — don't double-warn.
    sec = candidate.get("sec_alert") or {}
    if sec.get("severity", "").lower() in ("high", "critical"):
        return None
    return {
        "severity": "CAUTION",
        "reasoning": (
            f"News cluster: {len(news)} recent items. Information "
            "event not pinned by our SEC pipeline — defensive sizing."
        ),
    }
