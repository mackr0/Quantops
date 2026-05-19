"""CAUTION LONG when the portfolio is ALREADY long this name —
single-name concentration risk."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "portfolio_already_long"
DESCRIPTION = "CAUTION LONG when portfolio already long this name"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


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
        if qty > 0:
            return {"severity": "CAUTION",
                    "reasoning": f"Portfolio already long {sym} ({qty:.0f} shares). Adding compounds single-name concentration risk."}
    return None
