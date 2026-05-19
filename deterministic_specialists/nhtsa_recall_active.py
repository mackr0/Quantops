"""CAUTION LONG when an auto OEM has recent NHTSA recalls.

Recalls hit margin (campaign cost), brand equity, and can cascade
into class-actions. The market often under-reacts to the initial
filing — by the time the rolling cost shows up in earnings the
stock has already drifted lower.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "nhtsa_recall_active"
DESCRIPTION = "CAUTION LONG when auto OEM has recent NHTSA recalls"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    nhtsa = alt.get("nhtsa_recalls") or {}
    n = nhtsa.get("recalls_recent_years", 0) or 0
    if n <= 0:
        return None
    return {
        "severity": "CAUTION",
        "reasoning": (
            f"{n} recent NHTSA recall(s). Campaign cost + class-action "
            "risk; market often under-reacts initially."
        ),
    }
