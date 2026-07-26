"""CAUTION when expected slippage is high.

If the size-aware slippage estimator says we'll lose >30 bps to
execution, the edge has to be that much bigger to clear friction.
For thin candidates this often kills marginal trades.

P1.4 (2026-07-26): reads the structured `slippage_estimate` dict the
pipeline attaches (total_bps), falling back to parsing the prompt
string "exec cost ~X bps (...)". The original parsed a '%' pattern
the renderer stopped emitting — dead since the format change.
"""
from __future__ import annotations
import re
from typing import Any, Dict, Optional

NAME = "slippage_high_caution"
DESCRIPTION = "CAUTION when slippage estimate > 30 bps (friction may eat edge)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")
_SLIP_THRESHOLD_BPS = 30.0
_BPS_RE = re.compile(r"([\d.]+)\s*bps")


def slippage_bps(candidate: Dict[str, Any]) -> Optional[float]:
    """Expected slippage in bps: structured estimate first, prompt
    string second. None when neither is present/parsable."""
    est = candidate.get("slippage_estimate")
    if isinstance(est, dict) and not est.get("error"):
        try:
            v = float(est.get("total_bps"))
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    m = _BPS_RE.search(str(candidate.get("slippage_str") or ""))
    if m:
        try:
            return float(m.group(1))
        except (TypeError, ValueError):
            return None
    return None


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    v = slippage_bps(candidate)
    if v is None or v < _SLIP_THRESHOLD_BPS:
        return None
    return {
        "severity": "CAUTION",
        "reasoning": (
            f"Expected slippage {v:.0f} bps — friction eats the edge unless "
            "the directional move is materially larger."
        ),
    }
