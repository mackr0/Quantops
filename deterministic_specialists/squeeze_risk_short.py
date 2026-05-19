"""VETO SHORT when short-squeeze risk is HIGH.

If short interest is already crowded into a small float, the
shorts are positioning AGAINST the squeeze rather than for any
fundamental thesis. Adding to a HIGH squeeze-risk short is an
asymmetric loser — the upside is limited (price → 0) while the
downside is unbounded (squeeze can 2-3x the stock).
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "squeeze_risk_short"
DESCRIPTION = "VETO SHORT when squeeze risk is HIGH"
APPLIES_TO_SIGNALS = ("SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    risk = candidate.get("_squeeze_risk")
    if risk != "HIGH":
        return None
    return {
        "severity": "VETO",
        "reasoning": (
            "Squeeze risk HIGH on short side. Asymmetric loser: "
            "upside capped, downside unbounded."
        ),
    }
