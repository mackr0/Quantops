"""CONFIRM LONG when put/call ratio shows extreme retail panic
(contrarian buy signal)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "options_pcr_panic"
DESCRIPTION = "CONFIRM LONG when put/call ratio > 1.5 (retail panic)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    opts = alt.get("options") or {}
    pcr = opts.get("put_call_ratio")
    if pcr is None:
        return None
    try:
        v = float(pcr)
    except (TypeError, ValueError):
        return None
    if v >= 1.5:
        return {"severity": "CONFIRM",
                "reasoning": f"P/C ratio {v:.2f} — retail panic. Contrarian buy historically marks short-term lows."}
    return None
