"""CAUTION LONG when Google Trends search spike is detected
(attention-driven move; retail piling in)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "google_trends_spike"
DESCRIPTION = "CAUTION LONG when Google Trends spike (attention-driven entry)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    gt = alt.get("google_trends") or {}
    if not gt.get("has_spike") and not gt.get("is_spike"):
        return None
    pct = gt.get("spike_pct") or gt.get("change_pct") or 0
    return {"severity": "CAUTION",
            "reasoning": f"Google Trends spike detected (+{pct}%). Attention-driven retail entry — fade-the-attention bias is well documented."}
