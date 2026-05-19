"""CONFIRM LONG when there are 3+ insider buys recently (even
without the formal "cluster" tag)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "insider_recent_buys_meaningful"
DESCRIPTION = "CONFIRM LONG when 3+ recent insider buys (meaningful net activity)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    ins = alt.get("insider") or {}
    buys = ins.get("recent_buys", 0) or 0
    sells = ins.get("recent_sells", 0) or 0
    if buys < 3:
        return None
    if buys <= sells:  # not net positive
        return None
    return {"severity": "CONFIRM",
            "reasoning": f"Insiders net-buying: {buys} buys vs {sells} sells in last 30d. Meaningful directional vote."}
