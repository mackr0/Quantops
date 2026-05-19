"""CAUTION LONG when insiders have net-sold recently.

Insider selling has weaker signal than insider buying (sells are
often for diversification or taxes, not bearish conviction), but
NET-SOLD on >=3 transactions is more meaningful than a single
liquidity sale. Combined with a BUY signal from the system, the
operator should know the smart money is taking the other side.

CAUTION, not VETO: insider selling alone shouldn't kill a setup
with strong technicals + catalyst.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


NAME = "insider_sold_recently"
DESCRIPTION = "CAUTION LONG when insiders net-sold with ≥3 transactions in last 30 days"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")

_MIN_TRANSACTIONS = 3


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    insider = alt.get("insider") or {}
    if not insider:
        return None
    direction = insider.get("net_direction")
    sells = insider.get("recent_sells", 0) or 0
    buys = insider.get("recent_buys", 0) or 0
    total = sells + buys
    if direction == "selling" and total >= _MIN_TRANSACTIONS:
        return {
            "severity": "CAUTION",
            "reasoning": (
                f"Insiders net-selling: {sells} sells vs {buys} buys "
                f"in last 30d. Smart money taking the other side of "
                "the BUY signal."
            ),
        }
    return None
