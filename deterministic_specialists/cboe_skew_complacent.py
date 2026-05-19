"""CAUTION LONG when CBOE SKEW signals complacency (no tail-risk
priced in) — the contrarian case for defensive sizing in the
absence of priced fear."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "cboe_skew_complacent"
DESCRIPTION = "CAUTION LONG when CBOE SKEW is LOW (complacency)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    skew = ((alt.get("macro") or {}).get("cboe_skew")) or {}
    sig = (skew.get("skew_signal") or "").lower()
    if sig not in ("low", "very_low", "complacent"):
        return None
    return {"severity": "CAUTION",
            "reasoning": f"CBOE SKEW {sig} — complacency. When no one is paying for tail protection, that's often when surprises hit."}
