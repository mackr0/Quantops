"""CAUTION when bid-ask spread is wide (proxy: slippage estimate
> 0.15% even for moderate sizes — indicates thin order book)."""
from __future__ import annotations
import re
from typing import Any, Dict, Optional

NAME = "wide_spread_caution"
DESCRIPTION = "CAUTION when slippage estimate > 0.15% (wide-spread proxy)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")
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
    # Below 0.30 (slippage_high_caution threshold) but above 0.15
    if 0.15 <= v < 0.30:
        return {"severity": "CAUTION",
                "reasoning": f"Spread/slippage proxy {v:.2f}% — thin order book; bid-ask widens on size."}
    return None
