"""CONFIRM LONG when short-squeeze setup conditions stack: high
SI + small-float proxy (high SI requires small float for squeeze
dynamics) + bullish signal."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "short_squeeze_setup"
DESCRIPTION = "CONFIRM LONG on short-squeeze setup (high SI + MED/HIGH squeeze_risk)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    short = alt.get("short") or {}
    si = short.get("short_pct_float")
    risk = short.get("squeeze_risk", "")
    if si is None:
        return None
    try:
        s = float(si)
    except (TypeError, ValueError):
        return None
    if s >= 20 and risk in ("MED", "HIGH"):
        return {"severity": "CONFIRM",
                "reasoning": f"Short squeeze setup: SI {s:.0f}% + squeeze_risk {risk}. Coverage cascade can multiply directional gains."}
    return None
