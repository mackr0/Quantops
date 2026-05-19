"""CONFIRM signal when unusual options activity is detected.

Unusual options activity (UOA) is the classic "smart money saw
something" signal. Pair with directional confirmation: if the
option flow direction aligns with the candidate signal, the LLM
should weigh the convergence positively.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "unusual_options_activity"
DESCRIPTION = "CONFIRM when unusual options flow aligns with signal direction"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    opts = alt.get("options") or {}
    if not opts.get("unusual"):
        return None
    flow_signal = (opts.get("signal") or "neutral").lower()
    sig = (candidate.get("signal") or "").upper()
    pcr = opts.get("put_call_ratio", 0)
    # Alignment check
    long_sigs = {"BUY", "STRONG_BUY", "WEAK_BUY"}
    short_sigs = {"SELL", "STRONG_SELL", "WEAK_SELL", "SHORT"}
    aligned = (
        (sig in long_sigs and flow_signal in ("bullish", "calls"))
        or (sig in short_sigs and flow_signal in ("bearish", "puts"))
    )
    if not aligned:
        return None
    return {
        "severity": "CONFIRM",
        "reasoning": (
            f"Unusual options activity: {flow_signal} flow (P/C {pcr:.2f}) "
            "aligns with signal direction. Smart money convergence."
        ),
    }
