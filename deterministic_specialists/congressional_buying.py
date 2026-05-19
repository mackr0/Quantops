"""CONFIRM LONG when members of Congress recently net-bought.

Congressional STOCK Act disclosures have historically shown
member outperformance (Eisinger, Ziobrowski et al). Net buying
on multiple disclosures is the higher-quality signal.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "congressional_buying"
DESCRIPTION = "CONFIRM LONG when members of Congress net-bought recently"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    cong = alt.get("congressional_recent") or alt.get("congressional") or {}
    direction = (cong.get("net_direction") or "").lower()
    if direction not in ("buying", "bullish", "net_buy"):
        return None
    count = cong.get("trades_60d") or cong.get("recent_transactions") or 0
    dollars = cong.get("dollar_volume_60d") or cong.get("total_value") or 0
    return {
        "severity": "CONFIRM",
        "reasoning": (
            f"Congressional net-buying: {count} disclosures, "
            f"~${dollars:,.0f} value. Documented member outperformance."
        ),
    }
