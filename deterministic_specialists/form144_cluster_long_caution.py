"""CAUTION LONG when two or more Form 144s were filed in the last 30 days.

Form 144 is an insider's notice of INTENT to sell restricted stock —
it is filed before the sale and before the Form 4 that reports it.
A cluster of them is supply coming. Source: altdata_tier4.get_form144.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "form144_cluster_long_caution"
DESCRIPTION = "CAUTION LONG when ≥2 Form 144 (insider intent to sell) in 30 days"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    f = alt.get("form144") or {}
    if not f.get("has_data") or not f.get("is_cluster_30d"):
        return None
    return {
        "severity": "CAUTION",
        "reasoning": (f"{f.get('count_30d', 0)} Form 144 filings in 30 days (latest "
                      f"{f.get('latest_days_ago', '?')}d ago): insiders have filed intent "
                      "to sell — supply ahead of the Form 4s."),
    }
