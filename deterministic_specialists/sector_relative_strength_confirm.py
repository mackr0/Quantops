"""CONFIRM LONG when stock is outperforming its sector by ≥3% in 5d.

Relative strength vs sector is the cleanest momentum filter — it
strips out the broad market move and isolates company-specific
alpha. Leaders within a strong sector tend to keep leading.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "sector_relative_strength_confirm"
DESCRIPTION = "CONFIRM LONG when stock 5d ≥ sector 5d + 3pp"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")
_RS_THRESHOLD = 3.0


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    rs = candidate.get("rel_strength")
    if not isinstance(rs, dict):
        return None
    try:
        rs_val = float(rs.get("relative_strength", 0))
    except (TypeError, ValueError):
        return None
    if rs_val < _RS_THRESHOLD:
        return None
    return {
        "severity": "CONFIRM",
        "reasoning": (
            f"Stock outperforming {rs.get('sector', '?')} sector by "
            f"+{rs_val:.1f}% over 5d. Sector leaders tend to keep leading."
        ),
    }
