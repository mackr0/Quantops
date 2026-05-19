"""VETO LONG when SEC filing alert flags HIGH/CRITICAL severity.

The SEC pipeline parses 10-K/10-Q/8-K filings and flags material
language changes. A HIGH/CRITICAL alert means the company itself
has disclosed something that materially changes the thesis.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "sec_alert_high_severity"
DESCRIPTION = "VETO LONG on HIGH/CRITICAL SEC filing alert"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    sec = candidate.get("sec_alert") or {}
    sev = (sec.get("severity") or "").lower()
    if sev not in ("high", "critical"):
        return None
    form = sec.get("form", "?")
    signal = sec.get("signal", "?")
    return {
        "severity": "VETO",
        "reasoning": (
            f"SEC alert {sev.upper()}/{signal} on {form}. The company "
            "itself has disclosed something material."
        ),
    }
