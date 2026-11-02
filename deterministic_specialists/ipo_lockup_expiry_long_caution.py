"""CAUTION LONG inside the IPO lock-up expiry window.

A recent listing's insiders and early holders become free to sell
about 180 days after the first trade; the days around that date are a
known supply event. Derived from our own bar history
(altdata_tier4.get_ipo_lockup) — no external source.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "ipo_lockup_expiry_long_caution"
DESCRIPTION = "CAUTION LONG within ±10 days of the estimated IPO lock-up expiry"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    lk = alt.get("ipo_lockup") or {}
    if not lk.get("has_data") or not lk.get("is_recent_listing") or not lk.get("in_lockup_window"):
        return None
    d = lk.get("days_to_lockup_expiry")
    when = (f"in {d} days" if isinstance(d, int) and d > 0
            else f"{abs(d)} days ago" if isinstance(d, int) and d < 0 else "today")
    return {
        "severity": "CAUTION",
        "reasoning": (f"IPO lock-up expiry est. {lk.get('lockup_expiry_est', '?')} ({when}; listed "
                      f"{lk.get('days_listed', '?')}d): early holders become free to sell."),
    }
