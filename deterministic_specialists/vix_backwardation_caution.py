"""CAUTION on any new entry when the VIX term structure is in backwardation.

VIX above VIX3M means the market prices more volatility now than in
three months — the stress configuration. Both sides get the flag:
longs face the drawdown, shorts the violent rallies that end these
episodes. Source: altdata_tier4.get_vix_term_structure (CBOE).
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "vix_backwardation_caution"
DESCRIPTION = "CAUTION (both sides) when VIX > VIX3M — term structure in backwardation"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY", "SELL", "STRONG_SELL", "SHORT", "STRONG_SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    macro = (candidate.get("alt_data") or {}).get("macro") or {}
    vt = macro.get("vix_term") or {}
    if not vt.get("has_data") or vt.get("structure") != "backwardation":
        return None
    return {
        "severity": "CAUTION",
        "reasoning": (f"VIX term structure inverted: VIX {vt.get('vix', 0):.1f} > VIX3M "
                      f"{vt.get('vix3m', 0):.1f} (ratio {vt.get('ratio_1m_3m', 0):.2f}) — "
                      "stress regime; size down, expect whipsaw."),
    }
