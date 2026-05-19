"""CONFIRM LONG when MOVE (treasury vol) is in low regime — rate
volatility is contained; long-duration / growth names work."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "macro_treasury_low_riskon"
DESCRIPTION = "CONFIRM LONG when MOVE in low regime (rate vol contained)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    move = (((alt.get("macro") or {}).get("cross_asset_vol")) or {}).get("move") or {}
    label = (move.get("p30d_label") or "").lower()
    if label not in ("low", "very low"):
        return None
    return {"severity": "CONFIRM",
            "reasoning": f"MOVE (treasury vol) {label}. Rate vol contained — long-duration/growth names get the easier tape."}
