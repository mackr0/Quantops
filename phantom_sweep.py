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
                "       order_id, fill_price FROM trades "
                "WHERE COALESCE(status, 'open') IN (?, ?, ?) "
                "  AND order_id IS NOT NULL AND order_id != '' "
                "  AND replace(substr(timestamp, 1, 19), 'T', ' ') < ?",
                (*_LIVE_STATUSES, cutoff),
            ).fetchall()
            for r in rows:
                counts["checked"] += 1
                # Evidence hierarchy (2026-08-08, the p211 BAC void):
                # a row carrying a broker-verified fill (fill_price
                # was backfilled from a SUCCESSFUL order read) was
                # booked on stronger evidence than any failed lookup —
                # it must never be voided by one.
                _has_fill = False
                try:
                    _has_fill = float(r["fill_price"] or 0) > 0
                except (TypeError, ValueError):
                    _has_fill = False
                verdict = _row_verdict(api, r["order_id"],
                                       TERMINAL_STATUSES,
                                       get_order_cached,
                                       has_fill_evidence=_has_fill)
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
    try:
        counts["resurrected"] = resurrect_wrongly_voided(
            ctx, api=api).get("resurrected", 0)
    except Exception:
        logger.exception(
            "wrongful-void resurrection failed for %s (non-fatal)",
            getattr(ctx, "display_name", db_path))
    return counts


# Dead statuses the net re-examines. 'rejected'/'done_for_day' rows
# cannot carry fills by Alpaca's own semantics, so they stay out.
_DEAD_RESURRECTABLE = ("canceled", "expired")


def resurrect_wrongly_voided(ctx, api=None,
                             max_age_days: int = 45) -> dict:
    """THE NET (2026-08-08, operator directive after the p211 BAC
    void): what belongs to a profile belongs to that profile — a
    journal row may only stay DEAD while the broker agrees nothing
    filled. Every cycle, every dead row's order is checked against
    broker truth; an order with FILLS is a position this profile owns,
    so the row is restored FROM THAT EVIDENCE, whatever writer killed
    it and whatever instrument it is. A wrongful void — by any code
    path, past or future — survives at most one cycle.

    Cheap by construction: terminal orders are cached forever in
    order_status_cache, so each dead row costs one API read once.
    Restored shape comes from the broker: qty=filled_qty,
    price=filled_avg_price; status is 'open' for entry-side rows
    (buy/short, or a sell-to-open option with no prior long lots of
    that contract) and 'pending_fill' for exits, which the
    update_fills state machine then closes through the normal FIFO.
    Every resurrection writes a CRITICAL audit alert — this net
    firing means some writer upstream destroyed evidence.
    """
    counts = {"checked": 0, "resurrected": 0}
    db_path = getattr(ctx, "db_path", None)
    if not db_path:
        return counts
    try:
        from order_status_cache import get_order_cached, rate_limited
        if rate_limited():
            return counts
        if api is None:
            from client import get_api
            api = get_api(ctx)
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=max_age_days)
                  ).strftime("%Y-%m-%d %H:%M:%S")
        with closing(sqlite3.connect(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            if not conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='trades'").fetchone():
                return counts
            rows = conn.execute(
                "SELECT id, timestamp, symbol, side, qty, price, "
                "       fill_price, status, order_id, occ_symbol "
                "FROM trades "
                "WHERE COALESCE(status, '') IN (?, ?) "
                "  AND order_id IS NOT NULL AND order_id != '' "
                "  AND replace(substr(timestamp, 1, 19), 'T', ' ') >= ?",
                (*_DEAD_RESURRECTABLE, cutoff),
            ).fetchall()
            for r in rows:
                counts["checked"] += 1
                try:
                    order = get_order_cached(api, r["order_id"])
                except Exception as _net_exc:
                    logger.debug(
                        "resurrection net: order %s unverifiable this "
                        "cycle (%s) — retried next pass",
                        r["order_id"], _net_exc)
                    continue
                if order is None:
                    logger.debug(
                        "resurrection net: broker returned None for "
                        "%s — retried next pass", r["order_id"])
                    continue
                try:
                    filled_qty = float(
                        getattr(order, "filled_qty", 0) or 0)
                    fill_price = float(
                        getattr(order, "filled_avg_price", 0) or 0)
                except (TypeError, ValueError) as _net_parse_exc:
                    logger.debug(
                        "resurrection net: malformed fill fields on "
                        "%s (%s) — skipped", r["order_id"],
                        _net_parse_exc)
                    continue
                if filled_qty <= 0:
                    continue  # genuinely unfilled — the void stands
                side = (r["side"] or "").lower()
                if side in ("buy", "short"):
                    new_status = "open"
                elif r["occ_symbol"]:
                    prior_longs = conn.execute(
                        "SELECT COUNT(*) FROM trades "
                        "WHERE occ_symbol = ? AND side = 'buy' "
                        "  AND COALESCE(status,'') NOT IN (?, ?) "
                        "  AND timestamp <= ? AND id != ?",
                        (r["occ_symbol"], *_DEAD_RESURRECTABLE,
                         r["timestamp"], r["id"]),
                    ).fetchone()[0]
                    # No long lots to close → this sell OPENED a short
                    # option position.
                    new_status = "open" if not prior_longs \
                        else "pending_fill"
                else:
                    new_status = "pending_fill"
                new_price = (fill_price if fill_price > 0
                             else (r["price"] or None))
                conn.execute(
                    "UPDATE trades SET status = ?, qty = ?, "
                    "  price = COALESCE(?, price), "
                    "  fill_price = COALESCE(NULLIF(fill_price,0), ?), "
                    "  pnl = NULL, "
                    "  reason = COALESCE(reason || ' | ', '') || ? "
                    "WHERE id = ?",
                    (new_status, filled_qty, new_price,
                     (fill_price if fill_price > 0 else None),
                     f"RESURRECTED {datetime.now(timezone.utc).date()}: "
                     f"row was dead ({r['status']}) but broker order "
                     f"has filled_qty={filled_qty:g} @ "
                     f"{fill_price:g} — a filled order is a real "
                     f"position; restored from broker evidence",
                     r["id"]),
                )
                conn.commit()
                counts["resurrected"] += 1
                logger.error(
                    "WRONGFUL VOID RESURRECTED: trade #%s (%s %s "
                    "qty=%g, was %s) — broker order %s is FILLED "
                    "(%g @ %g). Restored to %s. A writer upstream "
                    "destroyed fill evidence; find it.",
                    r["id"], r["side"], r["occ_symbol"] or r["symbol"],
                    filled_qty, r["status"], r["order_id"],
                    filled_qty, fill_price, new_status,
                )
                try:
                    from halt_helpers import _write_audit_alert
                    _write_audit_alert(
                        db_path, "wrongful_void_resurrected",
                        "critical",
                        f"{r['occ_symbol'] or r['symbol']}: dead "
                        "journal row restored — its broker order is "
                        "FILLED",
                        (f"Row #{r['id']} ({r['side']} qty "
                         f"{filled_qty:g}) was marked "
                         f"'{r['status']}' while broker order "
                         f"{r['order_id']} holds real fills "
                         f"({filled_qty:g} @ {fill_price:g}). "
                         "Restored from broker evidence; the books "
                         "are true again. The writer that voided it "
                         "has a fill-evidence bug — investigate."),
                    )
                except Exception:
                    logger.exception(
                        "resurrection audit alert failed (row healed)")
    except Exception:
        logger.exception(
            "resurrect_wrongly_voided failed for %s (non-fatal)",
            getattr(ctx, "display_name", db_path))
    return counts


def _row_verdict(api, order_id, terminal_statuses,
                 get_order_cached,
                 has_fill_evidence: bool = False) -> Optional[str]:
    """Reason string when the row is a broker-confirmed phantom;
    None to skip (live / filled / unverifiable this cycle).

    2026-08-08 — EVIDENCE HIERARCHY (the p211 BAC 60P void: one
    transient 404 destroyed a live short position whose fill had
    already been broker-verified, orphaning -3 contracts). Two rules:
      1. A row with fill evidence is NEVER voided on a failed lookup —
         it was booked on a successful read; a failed read is weaker
         evidence, and a contradiction is an operator alarm, not a
         silent void.
      2. A 404 only voids after a DIRECT cache-bypassing confirm — a
         single flaky read (or a stale cached error) proves nothing.
    """
    def _confirmed_unknown() -> bool:
        # Direct, uncached read. Success or a non-404 error both mean
        # "NOT confirmed unknown".
        try:
            return api.get_order(order_id) is None
        except Exception as _direct_exc:
            _ds = str(_direct_exc).lower()
            return "not found" in _ds or "404" in _ds

    try:
        order = get_order_cached(api, order_id)
    except Exception as exc:
        s = str(exc).lower()
        if "not found" in s or "404" in s:
            if has_fill_evidence:
                logger.error(
                    "phantom sweep: order %s reads 404 but its row "
                    "carries a broker-verified fill — REFUSING to "
                    "void (evidence hierarchy); investigate if this "
                    "persists.", order_id,
                )
                return None
            if _confirmed_unknown():
                return ("order UNKNOWN at broker "
                        "(404, cache-bypassed confirm)")
        return None
    if order is None:
        if has_fill_evidence:
            logger.error(
                "phantom sweep: broker returned None for order %s but "
                "its row carries a broker-verified fill — REFUSING to "
                "void (evidence hierarchy).", order_id,
            )
            return None
        if _confirmed_unknown():
            return "broker returned None for order id (confirmed)"
        return None
    status = (getattr(order, "status", "") or "").lower()
    try:
        filled_qty = float(getattr(order, "filled_qty", 0) or 0)
    except (TypeError, ValueError):
        filled_qty = 0.0
    if (status in terminal_statuses and status != "filled"
            and filled_qty == 0):
        if has_fill_evidence:
            # The journal says a fill was verified; the broker order
            # now says zero fills. That contradiction is exactly what
            # must never be resolved silently in the destructive
            # direction.
            logger.error(
                "phantom sweep: order %s is terminal-unfilled at the "
                "broker but its row carries fill_price — REFUSING to "
                "void; operator review required.", order_id,
            )
            return None
        return f"terminal-unfilled at broker (status={status})"
    return None
