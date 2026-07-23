"""Automatic phantom-row sweep — the permanent form of the 2026-07-22
repair scripts.

THE INVARIANT: no live journal row (open / pending_fill /
pending_protective) may silently disagree with the broker for more
than one cycle. The 429-storm era proved what happens otherwise:
journal-only rows (orders that died after the row was written, or
never filled) accumulate invisibly, then surface through the integrity
gate ONE FINDING AT A TIME — each one latching the kill switch, each
one needing a hand-run repair script (the GOOG close pair, the STX
phantom short). This module is those scripts' verdict logic promoted
to an always-on, per-cycle sweep.

Verdict per row, broker-confirmed and fail-safe (never guesses):
  * order UNKNOWN at the broker (404/not found)   -> VOID
  * order terminal-but-unfilled (expired/canceled/
    rejected with filled_qty == 0)                -> VOID
  * order live, filled, or lookup failed          -> SKIP (retry next
    cycle; a filled order's row belongs to the fill machinery)

VOID = status='canceled', price=0 — the decomposition, the virtual
books, and the FIFO all exclude canceled rows, so a voided phantom
stops distorting equity immediately and the integrity gate re-certifies
on its next pass without operator action.

Age-gated (default 24h): a fresh order's broker-side propagation delay
can never void a real trade. Budget-safe: lookups go through
order_status_cache (terminal-forever + TTL + the 429 breaker), and the
sweep stands down entirely while the breaker is open.

Wired in two places (pinned by tests):
  1. the tail of `_task_update_fills` — every profile, every cycle;
  2. `_heal_pending_fills_best_effort` — the integrity gate's
     heal-BEFORE-halt step, so the gate only ever halts on books this
     sweep genuinely cannot explain.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_MAX_AGE_HOURS = 24

_LIVE_STATUSES = ("open", "pending_fill", "pending_protective")


def sweep_profile(ctx, api=None,
                  max_age_hours: int = DEFAULT_MAX_AGE_HOURS) -> dict:
    """Void every broker-confirmed phantom row in this profile's
    journal. Returns {"checked", "voided", "skipped"}. Never raises —
    a sweep failure must never break the cycle that hosts it."""
    counts = {"checked": 0, "voided": 0, "skipped": 0}
    db_path = getattr(ctx, "db_path", None)
    if not db_path:
        return counts
    try:
        from order_status_cache import (
            get_order_cached, rate_limited, TERMINAL_STATUSES)
        if rate_limited():
            # Breaker open — the API budget belongs to essential calls.
            # Phantoms are >24h old by definition; one more cycle of
            # patience is free.
            return counts
        if api is None:
            from client import get_api
            api = get_api(ctx)
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(hours=max_age_hours)
                  ).strftime("%Y-%m-%d %H:%M:%S")
        with closing(sqlite3.connect(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            if not conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='trades'").fetchone():
                return counts
            rows = conn.execute(
                "SELECT id, timestamp, symbol, side, qty, status, "
                "       order_id FROM trades "
                "WHERE COALESCE(status, 'open') IN (?, ?, ?) "
                "  AND order_id IS NOT NULL AND order_id != '' "
                "  AND replace(substr(timestamp, 1, 19), 'T', ' ') < ?",
                (*_LIVE_STATUSES, cutoff),
            ).fetchall()
            for r in rows:
                counts["checked"] += 1
                verdict = _row_verdict(api, r["order_id"],
                                       TERMINAL_STATUSES,
                                       get_order_cached)
                if verdict is None:
                    counts["skipped"] += 1
                    continue
                conn.execute(
                    "UPDATE trades SET status = 'canceled', price = 0 "
                    "WHERE id = ?", (r["id"],))
                conn.commit()
                counts["voided"] += 1
                logger.warning(
                    "phantom sweep: VOIDED trade #%s (%s %s qty=%s, "
                    "was %s) — %s. The row claimed a trade the broker "
                    "never executed.",
                    r["id"], r["side"], r["symbol"], r["qty"],
                    r["status"], verdict,
                )
    except Exception:
        logger.exception("phantom sweep failed for %s (non-fatal)",
                         getattr(ctx, "display_name", db_path))
    return counts


def _row_verdict(api, order_id, terminal_statuses,
                 get_order_cached) -> Optional[str]:
    """Reason string when the row is a broker-confirmed phantom;
    None to skip (live / filled / unverifiable this cycle)."""
    try:
        order = get_order_cached(api, order_id)
    except Exception as exc:
        s = str(exc).lower()
        if "not found" in s or "404" in s:
            return "order UNKNOWN at broker (404)"
        return None
    if order is None:
        return "broker returned None for order id"
    status = (getattr(order, "status", "") or "").lower()
    try:
        filled_qty = float(getattr(order, "filled_qty", 0) or 0)
    except (TypeError, ValueError):
        filled_qty = 0.0
    if (status in terminal_statuses and status != "filled"
            and filled_qty == 0):
        return f"terminal-unfilled at broker (status={status})"
    return None
