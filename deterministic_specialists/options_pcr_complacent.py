"""CAUTION LONG when put/call ratio shows extreme complacency."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "options_pcr_complacent"
DESCRIPTION = "CAUTION LONG when put/call ratio < 0.5 (complacency)"
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
    if 0 < v <= 0.5:
        return {"severity": "CAUTION",
                "reasoning": f"P/C ratio {v:.2f} — option market complacent; surprises tend to be downside in low-PCR regimes."}
    return None
