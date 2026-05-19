"""CONFIRM LONG on quality-factor exposure (positive earnings
revisions + reasonable PE + positive ROC). Classic "quality" factor
from MSCI / AQR factor models."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "quality_factor_long"
DESCRIPTION = "CONFIRM LONG on quality factor (positive revisions + sensible PE + positive ROC)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    est = alt.get("analyst_estimates") or {}
    f = alt.get("fundamentals") or {}
    pe = f.get("pe_trailing") or f.get("pe_ratio")
    roc = candidate.get("roc_10")
    direction = (est.get("eps_revision_direction") or "").lower()
    if pe is None or roc is None or direction != "up":
        return None
    try:
        p = float(pe); r = float(roc)
    except (TypeError, ValueError):
        return None
    if 8 <= p <= 30 and r > 0:
        return {"severity": "CONFIRM",
                "reasoning": f"Quality factor stack: EPS revisions UP + PE {p:.1f} + ROC10 {r:+.1f}%."}
    return None
