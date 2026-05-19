"""CONFIRM LONG when insiders bought within 30 days of earnings.

Insiders buying ahead of their own earnings call have strong
historical alpha — they're forbidden from buying on material
non-public info, but small buys around quiet-period boundaries
have consistently preceded beats.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "insider_buying_near_earnings"
DESCRIPTION = "CONFIRM LONG when insiders bought near earnings"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    ie = alt.get("insider_earnings") or {}
    if not ie.get("insider_buying_near_earnings"):
        return None
    d2e = ie.get("days_to_earnings", "?")
    return {"severity": "CONFIRM",
            "reasoning": f"Insiders bought {d2e}d before earnings — historically alpha-rich window."}
