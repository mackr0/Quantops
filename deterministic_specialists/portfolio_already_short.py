"""CAUTION SHORT when the portfolio is ALREADY short this name."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "portfolio_already_short"
DESCRIPTION = "CAUTION SHORT when portfolio already short this name"
APPLIES_TO_SIGNALS = ("SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    port = candidate.get("_portfolio") or {}
    positions = port.get("positions") or []
    sym = (candidate.get("symbol") or "").upper()
    if not sym:
        return None
    for p in positions:
        if (p.get("symbol") or "").upper() != sym:
            continue
        try:
            qty = float(p.get("qty", 0))
        except (TypeError, ValueError):
            continue
        if qty < 0:
            return {"severity": "CAUTION",
                    "reasoning": f"Portfolio already short {sym} ({qty:.0f} shares). Adding compounds single-name short concentration."}
    return None
