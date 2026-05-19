"""VETO LONG when 2+ negative catalysts stack (SEC alert, FDA
warning, NHTSA recall, EPA/OSHA violation, risk-factor diff)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "multiple_negative_catalysts"
DESCRIPTION = "VETO LONG when 2+ negative catalysts stack"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    flags = []
    if (candidate.get("sec_alert") or {}).get("severity", "").lower() in ("high", "critical"):
        flags.append("SEC")
    if ((alt.get("fda_inspections") or {}).get("recent_citations_count", 0) or 0) > 0:
        flags.append("FDA")
    if ((alt.get("nhtsa_recalls") or {}).get("recalls_recent_years", 0) or 0) > 0:
        flags.append("NHTSA")
    if ((alt.get("epa_osha_violations") or {}).get("epa_count", 0) or 0) > 0 or \
       ((alt.get("epa_osha_violations") or {}).get("osha_count", 0) or 0) > 0:
        flags.append("EPA/OSHA")
    if (alt.get("risk_factor_diff") or {}).get("has_new_risks"):
        flags.append("riskFactor")
    if len(flags) < 2:
        return None
    return {"severity": "VETO",
            "reasoning": f"Multiple negative catalysts stack: {' + '.join(flags)}. Compound regulatory/disclosure risk."}
