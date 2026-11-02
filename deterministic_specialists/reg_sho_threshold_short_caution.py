"""CAUTION SHORT when the name is on the Reg SHO threshold list.

Persistent fails-to-deliver (≥ 0.5% of shares outstanding for five
settlement days) mean the stock is already hard to deliver: forced
buy-ins and squeeze mechanics work against a fresh short. Source:
altdata_tier4.get_reg_sho_threshold (Nasdaq + NYSE daily lists).
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "reg_sho_threshold_short_caution"
DESCRIPTION = "CAUTION SHORT when on the Reg SHO threshold list (persistent FTDs)"
APPLIES_TO_SIGNALS = ("SELL", "STRONG_SELL", "SHORT", "STRONG_SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    rs = alt.get("reg_sho_threshold") or {}
    if not rs.get("has_data") or not rs.get("on_threshold_list"):
        return None
    venues = ", ".join(rs.get("venues") or []) or "exchange"
    return {
        "severity": "CAUTION",
        "reasoning": (f"On the Reg SHO threshold list ({venues}): persistent "
                      "fails-to-deliver — buy-in and squeeze risk for a new short."),
    }
