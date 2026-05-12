"""Per-symbol entry blacklist (2026-05-12 — Wave 8c).

When a symbol stops the trader out repeatedly, blacklist new entries
for a cool-off window. Stops the system from paying spread + slippage
to re-learn that a name isn't working in the current regime.

Storage: JSON column `entry_blacklist` on `trading_profiles`. Shape:
  {
    "NVDA": "2026-05-26T15:30:00",   # ISO expiry — naive UTC
    "TSLA": "2026-05-20T15:30:00",
    ...
  }

Helper API:
  - parse_blacklist(raw_json) — returns dict[symbol → expiry_iso],
    filters out expired entries
  - is_blacklisted(profile_or_dict, symbol) — bool
  - add_to_blacklist(profile_id, symbol, days=14) — persist a new
    cool-off entry

The trade pipeline calls `is_blacklisted(ctx, symbol)` before opening
a new BUY/SHORT and skips when True. Auto-expiry on read means stale
entries never accumulate.

Self-tuning (`_optimize_stop_out_blacklist`) populates this from the
trades table: 3+ stop_loss / trailing_stop / short_stop_loss exits
on the same symbol within the last 30 days → blacklist for 14 days.
AI-tunable: threshold count + cool-off window.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def parse_blacklist(raw_json: Optional[str]) -> Dict[str, str]:
    """Parse the raw `entry_blacklist` column.

    Returns {symbol: expiry_iso}, dropping malformed entries and
    entries whose expiry has passed. Returns {} on missing/invalid
    JSON. Symbols are upper-cased.
    """
    if not raw_json:
        return {}
    try:
        d = json.loads(raw_json)
    except (ValueError, TypeError):
        return {}
    if not isinstance(d, dict):
        return {}
    now = datetime.utcnow()
    out: Dict[str, str] = {}
    for sym, expiry in d.items():
        if not isinstance(sym, str) or not isinstance(expiry, str):
            continue
        try:
            dt = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                dt = dt.replace(tzinfo=None)
        except Exception:
            continue
        if dt > now:
            out[sym.upper()] = dt.isoformat()
    return out


def is_blacklisted(profile_or_dict: Any, symbol: str) -> bool:
    """True iff `symbol` has an active blacklist entry on this
    profile. Auto-expires stale entries on the read path."""
    if not symbol:
        return False
    if isinstance(profile_or_dict, dict):
        raw = profile_or_dict.get("entry_blacklist")
    else:
        raw = getattr(profile_or_dict, "entry_blacklist", None)
    bl = parse_blacklist(raw if isinstance(raw, str) else None)
    return symbol.upper() in bl


def add_to_blacklist(profile_id: int, symbol: str,
                       days: int = 14) -> bool:
    """Persist a new blacklist entry for `symbol` on `profile_id`.
    Returns True on success, False on any failure (logged).

    Idempotent — re-adding the same symbol REPLACES the expiry with
    a fresh `days`-from-now date. So 3 stop-outs over a week followed
    by a 4th doesn't double the cool-off; each new violation
    refreshes it.

    Uses `update_trading_profile` so the normal allowed_cols
    machinery sees it and logs rejections cleanly.
    """
    if not profile_id or not symbol:
        return False
    try:
        from models import (
            get_trading_profile, update_trading_profile,
        )
        prof = get_trading_profile(profile_id)
        if not prof:
            return False
        raw = prof.get("entry_blacklist") if isinstance(prof, dict) \
            else getattr(prof, "entry_blacklist", None)
        current = parse_blacklist(raw if isinstance(raw, str) else None)
        expiry = (datetime.utcnow() + timedelta(days=days)).isoformat()
        current[symbol.upper()] = expiry
        update_trading_profile(
            profile_id, entry_blacklist=json.dumps(current),
        )
        return True
    except Exception as exc:
        logger.warning(
            "add_to_blacklist(%s, %s): %s",
            profile_id, symbol, exc,
        )
        return False


def get_active_blacklist(profile_or_dict: Any) -> Dict[str, str]:
    """Return the live blacklist (auto-expired entries already
    filtered). Public read API for the dashboard."""
    if isinstance(profile_or_dict, dict):
        raw = profile_or_dict.get("entry_blacklist")
    else:
        raw = getattr(profile_or_dict, "entry_blacklist", None)
    return parse_blacklist(raw if isinstance(raw, str) else None)
