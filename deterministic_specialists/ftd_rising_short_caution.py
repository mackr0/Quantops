"""CAUTION SHORT when SEC fails-to-deliver are large and rising.

Fails piling up at the clearinghouse are delivery pressure building
under the price; a short added into that is on the wrong side of the
forced buying that clears them. Source: altdata_tier4.get_fails_to_deliver.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "ftd_rising_short_caution"
DESCRIPTION = "CAUTION SHORT when fails-to-deliver are large and rising"
APPLIES_TO_SIGNALS = ("SELL", "STRONG_SELL", "SHORT", "STRONG_SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    f = alt.get("fails_to_deliver") or {}
    if not f.get("has_data") or not f.get("reported"):
        return None
    if f.get("trend") != "rising" or not f.get("is_large"):
        return None
    return {
        "severity": "CAUTION",
        "reasoning": (f"Fails-to-deliver rising ({f.get('latest_fails', 0):,} shares on "
                      f"{f.get('latest_date', '?')}, max {f.get('max_fails', 0):,}) — "
                      "delivery pressure builds against a new short."),
    }
