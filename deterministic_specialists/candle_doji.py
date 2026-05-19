"""CAUTION on entries when today's bar is a doji (indecision).

Open ≈ close with meaningful range = market couldn't agree on
direction. After a trend, this signals exhaustion / pause. By
itself it's not a stop sign — but it argues against entering on
the same bar; wait for the next bar to confirm direction.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "candle_doji"
DESCRIPTION = "CAUTION on doji bar (body < 10% of range — indecision)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    c = (candidate.get("candle") or {}).get("today") or {}
    if not c or c.get("rng", 0) <= 0:
        return None
    body = c.get("body_pct", 0)
    # Doji = very small body but non-trivial range
    if body < 0.10:
        return {"severity": "CAUTION",
                "reasoning": f"Doji bar (body {body:.0%} of range) — market indecision. Wait for next bar to confirm direction."}
    return None
