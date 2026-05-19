"""CAUTION LONG when stock is underperforming its sector by ≥3%.

Mirror of `sector_relative_strength_confirm`. Stocks that lag
their sector tend to keep lagging — there's company-specific bad
news the broad sector tape hasn't picked up on.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "sector_weakness_caution"
DESCRIPTION = "CAUTION LONG when stock 5d ≤ sector 5d - 3pp"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")
_RS_FLOOR = -3.0


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    rs = candidate.get("rel_strength")
    if not isinstance(rs, dict):
        return None
    try:
        rs_val = float(rs.get("relative_strength", 0))
    except (TypeError, ValueError):
        return None
    if rs_val > _RS_FLOOR:
        return None
    return {
        "severity": "CAUTION",
        "reasoning": (
            f"Stock underperforming {rs.get('sector', '?')} sector by "
            f"{rs_val:+.1f}% over 5d. Company-specific weakness not "
            "yet reflected in the broad sector tape."
        ),
    }
