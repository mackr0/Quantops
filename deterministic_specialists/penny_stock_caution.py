"""CAUTION on any entry when price < $5 (penny / sub-$5 zone).

Sub-$5 stocks have systematically higher fraud, dilution, and
manipulation rates. Many institutional desks won't trade them.
For long entries the warning is dilution-risk; for shorts it's
the asymmetric upside-on-pump risk.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "penny_stock_caution"
DESCRIPTION = "CAUTION on any entry when price < $5"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    p = candidate.get("price")
    if p is None:
        return None
    try:
        pf = float(p)
    except (TypeError, ValueError):
        return None
    if 0 < pf < 5.0:
        return {"severity": "CAUTION",
                "reasoning": f"Price ${pf:.2f} — sub-$5 zone. Systematically higher fraud/dilution/manipulation risk."}
    return None
