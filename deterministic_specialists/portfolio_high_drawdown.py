"""CAUTION on entries when portfolio is in elevated drawdown
(>5% from peak) — risk-of-ruin tighter than usual."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "portfolio_high_drawdown"
DESCRIPTION = "CAUTION on entries when portfolio drawdown > 5%"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    port = candidate.get("_portfolio") or {}
    dd = port.get("drawdown_pct")
    if dd is None:
        return None
    try:
        v = float(dd)
    except (TypeError, ValueError):
        return None
    # Drawdown often reported as a positive number representing the drop
    if v < 5:
        return None
    return {"severity": "CAUTION",
            "reasoning": f"Portfolio drawdown {v:.1f}% from peak. Tighter risk-of-ruin window; size down or skip marginal candidates."}
