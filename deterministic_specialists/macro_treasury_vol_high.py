"""CAUTION LONG when MOVE (treasury vol) is elevated — interest-
rate sensitivity is rising; long-duration names hurt most."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "macro_treasury_vol_high"
DESCRIPTION = "CAUTION LONG when MOVE (treasury vol) is in high regime"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    move = (((alt.get("macro") or {}).get("cross_asset_vol")) or {}).get("move") or {}
    label = (move.get("p30d_label") or "").lower()
    if label not in ("high", "very high", "extreme"):
        return None
    return {"severity": "CAUTION",
            "reasoning": f"MOVE (treasury vol) {label}. Rate sensitivity rising; long-duration / growth names hit hardest."}
