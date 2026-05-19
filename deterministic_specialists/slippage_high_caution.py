"""CAUTION when expected slippage is high.

If the size-aware slippage estimator says we'll lose >0.3% to
execution, the edge has to be that much bigger to clear friction.
For thin candidates this often kills marginal trades.
"""
from __future__ import annotations
import re
from typing import Any, Dict, Optional

NAME = "slippage_high_caution"
DESCRIPTION = "CAUTION when slippage estimate > 0.3% (friction may eat edge)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")
_SLIP_THRESHOLD = 0.30
_PCT_RE = re.compile(r"([\d.]+)\s*%")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    slip = candidate.get("slippage_str")
    if not slip:
        return None
    m = _PCT_RE.search(str(slip))
    if not m:
        return None
    try:
        v = float(m.group(1))
    except (TypeError, ValueError):
        return None
    if v < _SLIP_THRESHOLD:
        return None
    return {
        "severity": "CAUTION",
        "reasoning": (
            f"Expected slippage {v:.2f}% — friction eats the edge unless "
            "the directional move is materially larger."
        ),
    }
