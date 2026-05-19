"""CAUTION LONG when the latest 10-K/Q added new risk factors.

Risk-factor section additions in 10-K/10-Q filings are the
company's own legal team flagging new material risks. This is
arguably the highest-signal section of a filing — companies don't
add risks lightly because they create disclosure liability.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "risk_factor_diff_added"
DESCRIPTION = "CAUTION LONG when 10-K/Q added new risk factors"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    diff = alt.get("risk_factor_diff") or {}
    if not diff.get("has_new_risks"):
        return None
    n = diff.get("added_risk_count", 0) or 0
    if n <= 0:
        return None
    return {
        "severity": "CAUTION",
        "reasoning": (
            f"Latest 10-K/Q added {n} new risk factor(s). The company's "
            "own legal team is flagging material risks."
        ),
    }
