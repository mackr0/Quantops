"""CAUTION LONG when EPA/OSHA violations detected (regulatory
risk + ESG screen impact)."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "epa_osha_violations_present"
DESCRIPTION = "CAUTION LONG on recent EPA/OSHA violations"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    v = alt.get("epa_osha_violations") or {}
    epa = v.get("epa_count", 0) or 0
    osha = v.get("osha_count", 0) or 0
    if epa <= 0 and osha <= 0:
        return None
    return {"severity": "CAUTION",
            "reasoning": f"Recent regulatory hits: {epa} EPA + {osha} OSHA. Fines + ESG-screen exposure."}
