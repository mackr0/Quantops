"""Reconcile each profile's journal against broker truth.

The journal-broker drift problem: the periodic
_task_reconcile_trade_statuses used to read the journal as its own
source of truth for virtual profiles, so it could never detect drift.
On 2026-05-06 we found 40/126 (31%) "open" journal entries across
11 profiles were phantoms — entries that were canceled-without-fill,
or that the broker had already closed via a protective stop without
the journal getting the SELL/COVER row.

This module is the broker-aware reconcile that closes the loop:
  - cancel-without-fill — entry order canceled/expired/rejected
    with filled_qty=0. Mark journal status='canceled'.
  - broker-sold-via-stop (long) — entry filled, broker has 0 shares.
    Find matching broker SELL fill, INSERT a SELL row, mark BUY closed.
  - broker-covered-via-stop (short) — same pattern for shorts. Match
    a broker BUY fill, INSERT a COVER row, mark SHORT closed.
  - partial-sale drift (long) — broker has SOME shares but fewer than
    the journal claims; a stop fired for a portion. Backfill SELL
    rows for the closed portion. BUY stays open with reduced qty (the
    FIFO consumes the SELL from the lot — original BUY row qty isn't
    edited).
  - partial-cover drift (short) — symmetrical.
  - partial-fill on entry — entry order canceled with filled_qty>0.
    Update journal qty to filled_qty, fix the entry price to actual
    fill, then re-evaluate as a normal open position.
  - api errors — retry with exponential backoff before flagging
    ambiguous so a transient broker hiccup doesn't leave drift open.

Use:
  python3 reconcile_journal_to_broker.py            # dry-run
  python3 reconcile_journal_to_broker.py --apply    # write changes
  python3 reconcile_journal_to_broker.py --profile 11 --apply
  python3 reconcile_journal_to_broker.py --quiet    # cron-friendly
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Number of retries for transient broker API failures before flagging
# the entry as ambiguous. Each retry waits 2^attempt seconds.
_API_MAX_RETRIES = 3


def _build_backfill_reason(order_type: Optional[str],
                            exit_price: Optional[float],
                            entry_price: Optional[float],
                            side: str,
                            partial: bool) -> str:
    """Render a specific reason string for a reconcile-backfilled exit.

    Alpaca's `order_type` on the filled exit identifies which protective
    mechanism fired. Threading that into the journal's `reason` text
    means an operator looking at the trades dashboard immediately sees
    "trailing stop fired" / "take-profit hit" / "stop-loss hit" instead
    of a generic "protective order" label that's identical for all
    three exit kinds.

    Triggered by the 2026-05-19 NOW-position confusion: TP=$115.89,
    SL=$95.52, actual exit=$105.29. With the old generic label the
    operator could not tell from the journal which order fired — it
    was actually the trailing stop, not the TP or SL.

    Args:
        order_type: Alpaca order_type field from the broker fill
            (e.g., 'trailing_stop', 'stop', 'stop_limit', 'limit',
            'market'). May be None or '?' on lookup failure.
        exit_price: Fill price of the protective order. Optional —
            included in the message for trailing-stop attribution
            when present.
        entry_price: Entry price. Used to compute % move on exit
            when both prices are available.
        side: 'sell' (long close) or 'cover' (short cover). Wording
            differs slightly between the two so the trade history
            reads naturally.
        partial: True when this exit was a partial close (only some
            of the position filled at the protective level).

    Returns a single-line string suitable for the `reason` column.
    """
    side_verb = "exited" if side == "sell" else "covered"
    # "partially exited" reads naturally as adverb modifying verb; the
    # original generic message used the same form.
    partial_prefix = "partially " if partial else ""

    move_pct = None
    try:
        if exit_price and entry_price and float(entry_price) > 0:
            if side == "sell":
                move_pct = (float(exit_price) - float(entry_price)) / float(entry_price) * 100.0
            else:  # cover: profit when cover_price < short_price
                move_pct = (float(entry_price) - float(exit_price)) / float(entry_price) * 100.0
    except (TypeError, ValueError):
        move_pct = None
    move_suffix = f" ({move_pct:+.1f}%)" if move_pct is not None else ""

    ot = (order_type or "").lower()
    if ot == "trailing_stop":
        return (
            f"trailing stop fired — {partial_prefix}{side_verb} at "
            f"${float(exit_price):.2f}{move_suffix}; broker order "
            f"backfilled by reconcile"
        ) if exit_price else (
            f"trailing stop fired — {partial_prefix}{side_verb}; "
            f"broker order backfilled by reconcile"
        )
    if ot == "stop":
        # Long: stop-loss caps a drawdown. Short: stop above entry
        # caps an adverse rally.
        kind = "stop-loss" if side == "sell" else "stop"
        return (
            f"{kind} hit — {partial_prefix}{side_verb} at "
            f"${float(exit_price):.2f}{move_suffix}; broker order "
            f"backfilled by reconcile"
        ) if exit_price else (
            f"{kind} hit — {partial_prefix}{side_verb}; "
            f"broker order backfilled by reconcile"
        )
    if ot == "stop_limit":
        return (
            f"stop-limit triggered — {partial_prefix}{side_verb} at "
            f"${float(exit_price):.2f}{move_suffix}; broker order "
            f"backfilled by reconcile"
        ) if exit_price else (
            f"stop-limit triggered — {partial_prefix}{side_verb}; "
            f"broker order backfilled by reconcile"
        )
    if ot == "limit":
        # Long: limit close = take-profit. Short: limit cover at low
        # price = take-profit on the short.
        kind = "take-profit hit"
        return (
            f"{kind} — {partial_prefix}{side_verb} at "
            f"${float(exit_price):.2f}{move_suffix}; broker order "
            f"backfilled by reconcile"
        ) if exit_price else (
            f"{kind} — {partial_prefix}{side_verb}; "
            f"broker order backfilled by reconcile"
        )
    if ot == "market":
        # Market exits aren't usually a "protective" order — likely a
        # manual close, external AI close, or position-clearing. Label
        # honestly rather than calling it protective.
        return (
            f"market {side_verb} (manual/external close) — "
            f"{partial_prefix}filled at ${float(exit_price):.2f}"
            f"{move_suffix}; backfilled by reconcile"
        ) if exit_price else (
            f"market {side_verb} (manual/external close) — "
            f"{partial_prefix}backfilled by reconcile"
        )
    # Unknown / missing order_type — keep the legacy generic phrasing
    # so existing parsers that grep for "protective" still match.
    return (
        f"broker {side_verb} via protective order ({ot or 'unknown type'}) — "
        f"{partial_prefix}backfilled by reconcile"
    )


def _to_utc_iso(value) -> Optional[datetime]:
    """Coerce a journal timestamp (TEXT) to a UTC-aware datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value)
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _broker_qty_for(positions, symbol: str) -> float:
    """Return the broker's current qty for a symbol. Negative = short."""
    sym_u = (symbol or "").upper()
    for p in positions:
        if (getattr(p, "symbol", "") or "").upper() == sym_u:
            try:
                return float(getattr(p, "qty", 0) or 0)
            except Exception:
                return 0
    return 0


def _retrying_call(func, *args, **kwargs):
    """Call a broker API function with exponential-backoff retries on
    transient failure. Returns (result, exception_or_None)."""
    last_exc = None
    for attempt in range(_API_MAX_RETRIES):
        try:
            return func(*args, **kwargs), None
        except Exception as exc:
            last_exc = exc
            if attempt < _API_MAX_RETRIES - 1:
                time.sleep(2 ** attempt)
    return None, last_exc


# A3 PROFILE ISOLATION (2026-06-16) — `_find_matching_exit_fill` was
# DELETED. It searched the SHARED Alpaca account's order history for
# ANY exit on a symbol matching qty + timing, which on a shared
# account attributed siblings' SELLs/BUYs to the wrong profile (the
# BATL/PPCB oversells, SOUN drift, and the recurring reconciler
# synthesis halts). All fill attribution is now own-order-id-only:
# a close is recognized solely via THIS profile's own
# protective_*_order_id (and the replace-chain walk on those ids) in
# `_detect_protective_fill`. Anything unexplained is ambiguous and
# halts for operator review rather than silently consuming a
# sibling's fill. See PROFILE_ORDER_ISOLATION.md.


def _classify_long_phantom(api, row, broker_qty, used_sell_ids):
    """Return ('cancel'|'backfill'|'partial_entry'|'ambiguous', detail).

    Only called when broker_qty <= 0 — i.e. the long position is
    fully gone from the broker but journal still claims it open.
    For options, broker-side lookups use the OCC symbol (the journal
    row's `symbol` is the underlying).
    """
    sym = _lookup_symbol_for_row(row)
    qty = float(row["qty"] or 0)
    order_id = row["order_id"]
    ts = _to_utc_iso(row["timestamp"])
    if not order_id:
        return "ambiguous", {"reason": "no order_id in journal"}
    entry_order, exc = _retrying_call(api.get_order, order_id)
    if entry_order is None:
        return "ambiguous", {"reason": f"failed to fetch entry order after retries: {exc}"}
    entry_status = getattr(entry_order, "status", "?")
    try:
        entry_filled = float(getattr(entry_order, "filled_qty", 0) or 0)
    except Exception:
        entry_filled = 0

    if entry_status in ("canceled", "expired", "rejected") and entry_filled == 0:
        return "cancel", {"order_id": order_id, "entry_status": entry_status}
    if entry_status in ("canceled", "expired", "rejected") and entry_filled > 0:
        # Partial fill on entry. Treat the filled portion as real.
        return "partial_entry", {
            "order_id": order_id, "entry_status": entry_status,
            "actual_filled_qty": entry_filled,
            "entry_avg_fill_price": float(getattr(entry_order, "filled_avg_price", 0) or 0),
        }
    if entry_status == "filled":
        # Broker filled the BUY at some point. Now broker has 0 shares,
        # so a SELL must have happened — but A3 (2026-06-16): we no
        # longer FUZZY-search broker history for it. On a shared
        # account that grabbed siblings' SELLs (BATL/PPCB oversells).
        # A legitimate exit is explained by THIS profile's OWN
        # protective_*_order_id in _detect_protective_fill (checked
        # before we reach here). If nothing own explained it, the
        # close is unexplained — surface it as an orphan_close so the
        # safety net HALTs for review instead of consuming a sibling's
        # fill OR silently leaving the position diverged.
        return "orphan_close", {
            "reason": (
                "entry filled, broker flat, but no OWN journaled exit "
                "order_id explains the close (fuzzy cross-profile "
                "match removed — see PROFILE_ORDER_ISOLATION.md)"
            ),
        }
    return "ambiguous", {
        "reason": f"entry status={entry_status} filled_qty={entry_filled}",
    }


def _classify_short_phantom(api, row, broker_qty, used_cover_ids):
    """Mirror of _classify_long_phantom for shorts.

    A short journal entry stores side='short' (per
    P1.10 of LONG_SHORT_PLAN.md). When the broker covers via a
    buy-to-cover stop, the journal needs a 'cover' row. Otherwise
    the short stays "open" forever in get_virtual_positions.
    """
    sym = _lookup_symbol_for_row(row)
    qty = float(row["qty"] or 0)
    order_id = row["order_id"]
    ts = _to_utc_iso(row["timestamp"])
    if not order_id:
        return "ambiguous", {"reason": "no order_id in journal"}
    entry_order, exc = _retrying_call(api.get_order, order_id)
    if entry_order is None:
        return "ambiguous", {"reason": f"failed to fetch entry order after retries: {exc}"}
    entry_status = getattr(entry_order, "status", "?")
    try:
        entry_filled = float(getattr(entry_order, "filled_qty", 0) or 0)
    except Exception:
        entry_filled = 0

    if entry_status in ("canceled", "expired", "rejected") and entry_filled == 0:
        return "cancel", {"order_id": order_id, "entry_status": entry_status}
    if entry_status in ("canceled", "expired", "rejected") and entry_filled > 0:
        return "partial_entry", {
            "order_id": order_id, "entry_status": entry_status,
            "actual_filled_qty": entry_filled,
            "entry_avg_fill_price": float(getattr(entry_order, "filled_avg_price", 0) or 0),
        }
    if entry_status == "filled":
        # A3 (2026-06-16) — fuzzy cover-fill search removed (same
        # cross-profile theft as the long path). A legitimate cover
        # is explained by THIS short's OWN protective_*_order_id in
        # _detect_protective_fill. If nothing own explained it, the
        # cover is unexplained → orphan_close → safety net halts.
        return "orphan_close", {
            "reason": (
                "short entry filled, broker flat, but no OWN journaled "
                "cover order_id explains the close (fuzzy cross-profile "
                "match removed — see PROFILE_ORDER_ISOLATION.md)"
            ),
        }
    return "ambiguous", {
        "reason": f"entry status={entry_status} filled_qty={entry_filled}",
    }


def _all_journal_sell_order_ids(profile_ids: Iterable[int]) -> set:
    """Collect every order_id referenced by a SELL or COVER row across
    every profile's journal. Used to dedup the fallback match path so
    we don't attribute one broker fill to two different profiles.

    The exact bug this prevents (caught 2026-05-06): profile_4 SOLD
    AVGO 10 (order `1fd38138`). profile_11 also had a 10-share AVGO
    BUY open. The fallback found a SELL with qty=10 (the broker's
    `1fd38138`) and attributed it to profile_11 — so both journals
    pointed to the same broker fill, double-counting.
    """
    import sqlite3 as _sqlite3
    used = set()
    for p_id in profile_ids:
        db_path = f"/opt/quantopsai/quantopsai_profile_{p_id}.db"
        try:
            conn = _sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            for r in conn.execute(
                "SELECT order_id FROM trades "
                "WHERE side IN ('sell', 'cover') AND order_id IS NOT NULL"
            ):
                if r[0]:
                    used.add(r[0])
            conn.close()
        except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError) as _db_exc:
            # Per-DB used-order-id aggregation loop; one bad DB
            # shouldn't kill cross-profile reconcile. Surface for follow-up.
            logger.debug(
                "used-order-id aggregation failed for %s: %s: %s",
                db_path, type(_db_exc).__name__, _db_exc,
            )
            continue
    return used


_REPLACE_CHAIN_MAX_DEPTH = 50  # bumped 2026-06-04: with proactive
# sync_pending_protective_order_ids running every cycle, chain depth
# at fill time should be ~1. 50 is a generous safety margin; hitting
# it means the sync sweep isn't running OR Alpaca is replacing the
# order pathologically fast — logged CRITICAL so it's distinguishable
# from the more common "no protective order" path.
_REPLACE_TRANSIENT_STATUSES = frozenset({"replaced", "pending_replace"})


def walk_replace_chain_forward(api, start_oid: str,
                                max_depth: int = _REPLACE_CHAIN_MAX_DEPTH):
    """Follow `replaced_by` on an Alpaca order until we hit a terminal
    status (filled / canceled / expired / rejected / accepted / new),
    or run out of chain links, or exceed max_depth.

    Alpaca silently REPLACES trailing-stop orders as the trail bumps
    (server-side, no `submit_order` call). Each replacement has a
    fresh order_id; the parent's status becomes 'replaced' and exposes
    a `replaced_by` field pointing at the successor.

    When a trailing stop finally fires, the FILL lands under the
    terminal id in the chain — not the id we journaled at placement.
    To detect the fill from the journaled id, we walk forward.

    Returns (terminal_order, depth_walked).
      - terminal_order is the Alpaca order object at the end of the
        chain, or None if the walk could not complete (API error,
        broken chain, max depth exceeded).
      - depth_walked is how many `replaced_by` links we followed
        (0 means the start order was already terminal).
    """
    order, _exc = _retrying_call(api.get_order, start_oid)
    depth = 0
    while (order is not None
           and getattr(order, "status", "") in _REPLACE_TRANSIENT_STATUSES
           and depth < max_depth):
        next_id = getattr(order, "replaced_by", None)
        if not next_id:
            return None, depth
        order, _exc = _retrying_call(api.get_order, next_id)
        depth += 1
    if depth >= max_depth and order is not None and (
        getattr(order, "status", "") in _REPLACE_TRANSIENT_STATUSES
    ):
        # max_depth=50 hit: the proactive sync sweep should keep the
        # journal's order_id within 1-2 hops of the live id. If we're
        # still walking after 50 steps, either the sweep is broken or
        # something is wrong at Alpaca. CRITICAL because the fill (if
        # any) will surface as orphan + reconciler halt — the operator
        # needs to know the root cause is "chain too deep" not "no
        # journal row."
        logger.critical(
            "replace-chain walk hit max_depth=%d on start_oid=%s "
            "(terminal status=%s, replaced_by=%s). The proactive "
            "sync_pending_protective_order_ids sweep should keep "
            "this near 0; hitting %d indicates either the sweep "
            "isn't running OR a single order has been replaced >%d "
            "times. Any fill will surface as an orphan and halt the "
            "profile.",
            max_depth, start_oid, getattr(order, "status", "?"),
            getattr(order, "replaced_by", None),
            max_depth, max_depth,
        )
        return None, depth
    return order, depth


# Backwards-compat alias: existing call sites in this module + the
# 2026-06-04 test file import the underscore-prefixed name. Public
# name is preferred for new code (bracket_orders.sync_pending_*
# uses the un-prefixed version).
_walk_replace_chain_forward = walk_replace_chain_forward


def walk_replace_chain_backward(
    api,
    terminal_oid: str,
    target_oid: str,
    max_depth: int = _REPLACE_CHAIN_MAX_DEPTH,
) -> bool:
    """Walk an Alpaca order's `replaces` chain backward looking for
    `target_oid`. Returns True if found within max_depth, False if
    not (chain dead-ended, hit max_depth, or never linked).

    Used by `_detect_protective_fill`'s backward-traverse fallback:
    when forward walk from a journaled id dead-ends because an
    intermediate replace link has been GC'd, we can still match a
    candidate broker order to the journaled placement by walking
    backward from the candidate via `replaces`. If the trail leads
    to our journaled id, the candidate IS the terminal fill we want.
    """
    if not terminal_oid or not target_oid:
        return False
    if terminal_oid == target_oid:
        return True
    current_oid = terminal_oid
    for _ in range(max_depth):
        order, _exc = _retrying_call(api.get_order, current_oid)
        if order is None:
            return False
        prev_oid = (
            getattr(order, "replaces", None)
            or getattr(order, "replaced_id", None)
        )
        if not prev_oid:
            return False
        if prev_oid == target_oid:
            return True
        current_oid = prev_oid
    return False


def _find_terminal_via_backward_walk(
    api, row, journaled_oid: str, used_sell_ids: set,
) -> Optional[dict]:
    """Forward-walk fallback: when `walk_replace_chain_forward`
    dead-ends from the journaled id, search recent broker orders on
    this symbol for any filled SELL/BUY whose `replaces` chain
    traces back to the journaled id. This catches the case where
    Alpaca GC'd intermediate replace links and the forward walk
    can't reach the terminal.

    Returns the fill detail dict in the same shape as
    `_detect_protective_fill` produces, or None if no backward-walk
    match exists.
    """
    side = (row["side"] or "").lower()
    expected_exit_side = "buy" if side == "short" else "sell"
    sym = _lookup_symbol_for_row(row)
    if not sym:
        return None
    try:
        ts = _to_utc_iso(row["timestamp"]) or datetime.now(timezone.utc)
    except Exception:
        return None
    # Pull recent broker orders on this symbol. The fuzzy-fallback
    # path elsewhere uses the same shape; we reuse list_orders here
    # to get candidates without coupling to its exact filter.
    try:
        orders = api.list_orders(
            status="filled",
            symbols=[sym],
            after=ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
            direction="asc",
            limit=200,
        )
    except TypeError:
        # SDK that doesn't accept symbols=/after= — fall back to
        # full list + filter.
        try:
            orders = api.list_orders(status="filled", limit=500,
                                       direction="desc")
        except Exception:
            return None
    except Exception:
        return None

    for cand in orders or []:
        cand_id = getattr(cand, "id", None)
        if not cand_id or cand_id in used_sell_ids:
            continue
        if getattr(cand, "side", "") != expected_exit_side:
            continue
        if getattr(cand, "symbol", "") != sym:
            continue
        # Walk this candidate's `replaces` chain backward. If we
        # land on the journaled id, this is the terminal fill that
        # corresponds to our protective placement.
        if not walk_replace_chain_backward(api, cand_id, journaled_oid):
            continue
        try:
            filled_qty = float(getattr(cand, "filled_qty", 0) or 0)
            fill_price = float(getattr(cand, "filled_avg_price", 0) or 0)
        except (ValueError, TypeError, AttributeError):
            continue
        if filled_qty <= 0 or fill_price <= 0:
            continue
        filled_at = getattr(cand, "filled_at", None)
        if hasattr(filled_at, "isoformat"):
            fa_dt = (
                filled_at
                if filled_at.tzinfo
                else filled_at.replace(tzinfo=timezone.utc)
            )
        else:
            fa_dt = _to_utc_iso(filled_at)
        used_sell_ids.add(cand_id)
        return {
            # IMPORTANT: return the JOURNALED oid as `order_id` so
            # the apply path's UPDATE matches the pending_protective
            # row's order_id column. The terminal id was added to
            # used_sell_ids above to prevent double-attribution.
            "order_id": journaled_oid,
            "filled_at": fa_dt,
            "filled_qty": filled_qty,
            "filled_avg_price": fill_price,
            "order_type": getattr(cand, "order_type", "?"),
        }
    return None


def _is_bracket_child_fill(api, conn, action, fill_oid) -> bool:
    """True when `fill_oid` is a child leg of the bracket parent that
    opened `action`'s entry trade.

    2026-06-10 (PM) — bracket children are created broker-side as
    part of the parent submit; our code never calls submit_order for
    them, so they legitimately have no pending_protective row when
    the at-submit stamp/pending-write raced the broker's child
    materialization. The reconciler uses this check to classify such
    fills as EXPECTED protective synthesis instead of halting the
    profile. Precise (parent-child linkage by order id), no fuzzy
    matching. Returns False on any lookup failure — the caller then
    falls back to the conservative halt path."""
    try:
        entry = conn.execute(
            "SELECT order_id FROM trades WHERE id = ?",
            (action.get("trade_id"),),
        ).fetchone()
        entry_oid = entry[0] if entry else None
        if not entry_oid:
            return False
        parent = api.get_order(entry_oid, nested=True)
        if (getattr(parent, "order_class", "") or "") != "bracket":
            return False
        return any(
            getattr(leg, "id", None) == fill_oid
            for leg in (getattr(parent, "legs", None) or [])
        )
    except Exception as _bc_exc:
        logger.debug(
            "bracket-child fill check failed for %s (conservative "
            "halt path applies): %s: %s",
            fill_oid, type(_bc_exc).__name__, _bc_exc,
        )
        return False


def _detect_protective_fill(api, row, used_sell_ids):
    """For any open BUY/SHORT, check whether its OWN protective order
    fired at the broker — independent of the symbol's broker_qty.

    This is the key fix for the multi-profile aggregation bug found
    2026-05-06: when profile_9's trailing stop fires for GT 573, the
    broker still has 1399 GT from sibling profiles. Per-profile
    reconcile that gates on `broker_qty > 0` would say "real_held"
    and miss profile_9's exit entirely. By checking the BUY's OWN
    protective orders directly, we catch each profile's exit
    accurately regardless of sibling state.

    Two-layer detection:
      1. Look up the protective_*_order_id columns directly. Fast,
         precise. Walks the Alpaca REPLACE chain forward from the
         journaled id (trailing stops are silently replaced server-
         side as they trail). The fill data comes from the chain's
         terminal order; the returned `order_id` is the ORIGINAL
         journaled placement id so the reconciler's pending_protective
         UPDATE can still match by primary key.
      2. Fallback: search broker order history for SELLs/BUYs that
         match the journal qty, occurred after the BUY's timestamp,
         and aren't already attributed to a sibling profile.
         Catches the case where the protective_*_order_id is missing
         but a matching exit DID fire.

    Returns one of:
      ("backfill_full", detail) — protective fully closed the position;
        caller marks BUY status='closed' and inserts SELL row.
      ("backfill_partial", detail) — protective partially closed;
        caller inserts SELL row, BUY stays open (FIFO consumes lot).
      (None, None) — no protective order filled.
    """
    journal_qty = float(row["qty"] or 0)
    for col in ("protective_stop_order_id", "protective_tp_order_id",
                "protective_trailing_order_id"):
        try:
            stop_oid = row[col]
        except (KeyError, IndexError):
            stop_oid = None
        if not stop_oid:
            continue
        if stop_oid in used_sell_ids:
            continue
        order, _depth = _walk_replace_chain_forward(api, stop_oid)
        if order is None:
            # RC2 (2026-06-05): forward walk dead-ended (chain GC'd,
            # intermediate link missing, max_depth hit). Try the
            # backward-walk fallback: scan recent broker orders on
            # this symbol, walking each candidate's `replaces` chain
            # backward looking for stop_oid. If we find a candidate
            # whose backward trail reaches us, that candidate IS the
            # terminal fill we want.
            backward = _find_terminal_via_backward_walk(
                api, row, stop_oid, used_sell_ids,
            )
            if backward is not None:
                journal_qty = float(row["qty"] or 0)
                filled_qty = backward["filled_qty"]
                if abs(filled_qty - journal_qty) < 0.5:
                    return "backfill_full", backward
                if filled_qty < journal_qty:
                    return "backfill_partial", backward
            continue
        # When the walk traversed >= 1 replace link, also mark the
        # terminal id as used. The fuzzy fallback (and sibling-profile
        # reconciles via cross_profile_used_ids) matches broker fills
        # by id; without this they might double-attribute one fill.
        terminal_oid = getattr(order, "id", None)
        if (_depth > 0 and terminal_oid
                and terminal_oid != stop_oid):
            used_sell_ids.add(terminal_oid)
        if getattr(order, "status", "") != "filled":
            continue
        # Side check: longs exit via 'sell', shorts cover via 'buy'
        side = (row["side"] or "").lower()
        expected_exit_side = "buy" if side == "short" else "sell"
        if getattr(order, "side", "") != expected_exit_side:
            continue
        try:
            filled_qty = float(getattr(order, "filled_qty", 0) or 0)
        except (ValueError, TypeError, AttributeError) as _q_exc:
            # Per-order filled_qty parse loop; skip orders with
            # malformed broker response. Surface for follow-up.
            logger.debug(
                "reconcile filled_qty parse failed: %s: %s",
                type(_q_exc).__name__, _q_exc,
            )
            continue
        if filled_qty <= 0:
            continue
        try:
            fill_price = float(getattr(order, "filled_avg_price", 0) or 0)
        except (ValueError, TypeError, AttributeError) as _p_exc:
            # Per-order filled_avg_price parse loop; skip orders
            # with malformed broker response. Surface for follow-up.
            logger.debug(
                "reconcile filled_avg_price parse failed: %s: %s",
                type(_p_exc).__name__, _p_exc,
            )
            continue
        if fill_price <= 0:
            continue
        filled_at = getattr(order, "filled_at", None)
        if hasattr(filled_at, "isoformat"):
            fa_dt = filled_at if filled_at.tzinfo else filled_at.replace(tzinfo=timezone.utc)
        else:
            fa_dt = _to_utc_iso(filled_at)
        detail = {
            "order_id": stop_oid,
            "filled_at": fa_dt,
            "filled_qty": filled_qty,
            "filled_avg_price": fill_price,
            "order_type": getattr(order, "order_type", "?"),
        }
        # Full closure if filled_qty matches journal qty (within 0.5 tol)
        if abs(filled_qty - journal_qty) < 0.5:
            return "backfill_full", detail
        if filled_qty < journal_qty:
            return "backfill_partial", detail
        # filled_qty > journal_qty shouldn't happen for a single
        # profile's protective order — fall through and skip.
    #
    # A3 PROFILE ISOLATION (2026-06-16) — the fuzzy symbol/qty/time
    # fallback (`_find_matching_exit_fill`) is DELETED. On a SHARED
    # Alpaca account that search returned ANY profile's SELL/BUY for
    # the symbol matching qty + timing, so a sibling's exit was
    # attributed to THIS profile (BATL/PPCB oversells, SOUN drift).
    # Now A0 guarantees every exit's order_id is journaled, so a
    # legitimate close is ALWAYS explained by one of THIS profile's
    # OWN protective_*_order_id values handled above (or the
    # backward-walk on those same ids). If none matched, the close
    # is NOT ours to claim — return (None, None). The caller's
    # phantom path then treats it as ambiguous → the reconciler
    # safety net halts for operator review rather than silently
    # consuming a sibling's fill. See PROFILE_ORDER_ISOLATION.md.
    return None, None


# Backwards-compat alias (older test path expected this name)
_detect_partial_sale = _detect_protective_fill


def _open_journal_conn(db_path: str) -> sqlite3.Connection:
    """Open a journal connection with row_factory set. Caller is
    responsible for closing — used only by reconcile_with_ctx whose
    body spans ~300 lines and would be untenable to wrap in `with`."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _select_open_rows(conn) -> List[sqlite3.Row]:
    """Pull every open journal row (long + short, stocks AND options).

    Options rows have occ_symbol set; we route the broker lookup by
    that symbol instead of the underlying so the reconcile correctly
    finds the option position.

    Tolerate older schemas (missing protective_* / occ_symbol cols)
    by selecting columns dynamically."""
    cur = conn.execute("PRAGMA table_info(trades)")
    cols = {r[1] for r in cur.fetchall()}
    base_cols = ["id", "symbol", "side", "qty", "status", "order_id",
                 "timestamp", "price"]
    extra_cols = [c for c in (
        "protective_stop_order_id", "protective_tp_order_id",
        "protective_trailing_order_id",
        "occ_symbol", "option_strategy",
    ) if c in cols]
    all_cols = base_cols + extra_cols
    # Phase 5e (2026-05-12) — EXCLUDE rows tagged with a
    # data_quality marker from reconcile. The phantom-stop incident
    # rows have price=$0.16 (option premium) but signal_type='SELL'
    # and occ_symbol=NULL. The reconciler was reading them as
    # phantom long positions and "closing" them with today's stock
    # price → bogus Reconcile Backfill rows on the trades page
    # showing +4833% / +2450% / +1447% pnl_pct. Filtering at the
    # candidate-fetch boundary prevents NEW bogus rows from being
    # created.
    dq_clause = " AND data_quality IS NULL" if "data_quality" in cols else ""
    sql = (f"SELECT {','.join(all_cols)} FROM trades "
           f"WHERE status='open' AND side IN ('buy', 'short', 'sell'){dq_clause}")
    return conn.execute(sql).fetchall()


def _lookup_symbol_for_row(row) -> str:
    """Return the broker-side symbol for a journal row. Options use
    the OCC symbol (e.g. 'MSFT260612P00375000'); stocks use the
    underlying."""
    try:
        occ = row["occ_symbol"]
    except (KeyError, IndexError):
        occ = None
    if occ:
        return occ
    return (row["symbol"] or "").upper()


def reconcile_option_orphans(api, conn, positions, today,
                             apply_changes) -> list:
    """OPTION-ORPHAN BACKSTOP (2026-06-17). Per-cycle broker-truth pass
    that closes any OPEN option leg — LONG or SHORT, single-leg or
    multileg — that the broker no longer holds, regardless of cause:
    early assignment, early/auto exercise, manual/external close, or a
    close that never got journaled (O5/O6). This is the operator's
    "orphans are impossible" guarantee for options: even if every source
    path misses, the orphan is removed from the book next cycle.

    Why a DEDICATED pass (not the stock loop): the stock loop at the top
    of reconcile_with_ctx does `if side == 'sell': continue`, so it
    SKIPS every open short option leg entirely (O8) — short legs had no
    backstop at all. This pass covers both sides.

    SHARED-ACCOUNT SAFE — never consume a sibling's identical OCC:
    `api.list_positions()` returns ONE aggregated qty per OCC across all
    profiles sharing the Alpaca conduit. So this pass acts ONLY when the
    account-level OCC qty is ZERO — flat for EVERYONE implies flat for
    us, which is unambiguous. A non-zero OCC qty may be a sibling's
    contract, so those legs are LEFT to the fill-confirmation state
    machine (which closes a journaled own-close when it fills) and the
    expiry sweep. We never close a leg on a non-zero OCC, so we can
    never attribute a sibling's contract (the BATL/PPCB/SOUN
    cross-profile bug class on the option surface).

    EXPIRY HANDOFF: defers expiry<=today to
    options_lifecycle.sweep_expired_options (which owns ITM/OTM
    intrinsic value + assignment/exercise synthetic equity legs). Acts
    only on expiry>today or NULL — the exact inverse of the sweep — so
    the two never touch the same leg.

    Journal-side ONLY: never cancels or submits a broker order. P&L is
    NOT fabricated — the leg is flipped to 'auto_reconciled_phantom_close'
    (pnl=0), a status get_virtual_positions already excludes, so the
    orphan leaves the book immediately; the real assignment/exercise
    cash is booked idempotently by the broker-activities pass.

    Returns the list of closed legs (for the summary). These are
    EXPECTED reconciles — an option leg legitimately vanishes at the
    broker on every assignment/exercise — and must NOT count toward the
    synthesis HALT.
    """
    from datetime import date as _date
    closed: list = []
    try:
        _cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(trades)").fetchall()}
    except sqlite3.Error:
        _cols = set()
    if "occ_symbol" not in _cols:
        return closed  # no options on this (legacy/minimal) schema
    _sel = ["id", "symbol", "side", "qty", "occ_symbol"]
    _sel += [c for c in ("expiry", "order_id") if c in _cols]
    try:
        legs = conn.execute(
            f"SELECT {', '.join(_sel)} FROM trades "
            "WHERE occ_symbol IS NOT NULL "
            "AND COALESCE(status, 'open') = 'open'"
        ).fetchall()
    except sqlite3.Error as exc:
        logger.debug("reconcile_option_orphans select failed: %s", exc)
        return closed

    def _g(row, key):
        try:
            return row[key]
        except (KeyError, IndexError):
            return None

    for leg in legs:
        occ = _g(leg, "occ_symbol")
        # EXPIRY HANDOFF — expiry<=today belongs to the expiry sweep.
        exp = _g(leg, "expiry")
        if exp:
            try:
                if _date.fromisoformat(str(exp)[:10]) <= today:
                    continue
            except (ValueError, TypeError):
                pass  # unparseable expiry → treat as live, handle here
        # BROKER TRUTH — only act when the OCC is flat for EVERYONE.
        if abs(_broker_qty_for(positions, occ)) >= 0.001:
            # Broker still holds this OCC (ours or a sibling's on the
            # shared conduit) — not a confirmable orphan. Leave it for
            # fill-confirmation / the expiry sweep; never consume a
            # sibling's contract.
            continue
        # LIVE RE-CHECK — the fill-confirmation state machine may have
        # flipped this row terminal mid-pass (it closed cleanly).
        try:
            still_open = conn.execute(
                "SELECT 1 FROM trades WHERE id = ? "
                "AND COALESCE(status, 'open') = 'open'",
                (_g(leg, "id"),),
            ).fetchone()
        except sqlite3.Error:
            still_open = True
        if not still_open:
            continue
        # Distinguish a NEVER-OPENED entry (order canceled/expired/
        # rejected with 0 fill → 'canceled') from a filled-then-vanished
        # position (early close / assignment / exercise → auto-closed).
        # The entry-order fetch happens ONLY for the few account-flat
        # legs, never for held ones (the qty check above short-circuits).
        new_status = "auto_reconciled_phantom_close"
        new_pnl = 0.0
        kind = "auto_closed"
        reason = ("reconcile: broker flat for this OCC (early close / "
                  "assignment / exercise) — option leg auto-closed; "
                  "realized cash via activities capture")
        oid = _g(leg, "order_id")
        if oid:
            entry_order, _exc = _retrying_call(api.get_order, oid)
            if entry_order is not None:
                est = (getattr(entry_order, "status", "") or "").lower()
                try:
                    efilled = float(getattr(entry_order, "filled_qty", 0) or 0)
                except (TypeError, ValueError):
                    efilled = 0.0
                if (est in ("canceled", "expired", "rejected")
                        and efilled == 0):
                    new_status = "canceled"
                    new_pnl = None
                    kind = "canceled"
                    reason = ("reconcile: option entry order %s — "
                              "never filled; canceled." % est)
        detail = {
            "trade_id": _g(leg, "id"), "occ_symbol": occ,
            "symbol": _g(leg, "symbol"), "side": (_g(leg, "side") or ""),
            "qty": float(_g(leg, "qty") or 0), "kind": kind,
            "new_status": new_status,
        }
        if apply_changes:
            try:
                conn.execute(
                    "UPDATE trades SET status = ?, pnl = ?, "
                    "reason = COALESCE(reason || ' | ', '') || ? "
                    "WHERE id = ?",
                    (new_status, new_pnl, reason, _g(leg, "id")),
                )
                conn.commit()
            except sqlite3.Error as exc:
                logger.warning(
                    "reconcile_option_orphans: failed to close leg %s "
                    "(%s): %s", _g(leg, "id"), occ, exc,
                )
                continue
        logger.info(
            "Reconcile: option leg #%s %s broker-flat — %s (orphan "
            "removed; not a halt).", _g(leg, "id"), occ, kind,
        )
        closed.append(detail)
    return closed


class ReconcileUnavailable(Exception):
    """Raised by reconcile_and_stamp / ensure_symbol_fresh when a profile could
    NOT be reconciled to broker truth this cycle (broker unreachable after
    retries). The oversell door catches it and refuses the sell (fail-closed)
    rather than act on a possibly-stale book. Critical: reconcile_with_ctx
    RETURNS {"error": ...} on broker failure (it does not raise), so without
    this the door would treat a stale book as fresh and let a naked sell
    through — the exact divergence vector the invariant exists to kill."""


def _reconstruct_unjournaled_submits(ctx) -> int:
    """Durable-journaling recovery: recover orders the broker accepted but
    whose journal write was lost (submitted_orders rows with no trades row).

    For each, fetch the broker order by its OWN order_id:
      - filled ENTRY (buy/short) → write the trades row from broker truth so
        the position is OWNED and never seen as an orphan;
      - canceled / expired / rejected → drop the recovery record (never
        filled, nothing to recover);
      - a filled SELL/COVER, or a still-working order → leave it: a sell's
        close must go through the reconciler's FIFO matching (which it owns),
        and a working order will reconstruct once it fills.

    Only this profile's OWN order_ids are touched (the order-id-truth
    invariant). Best-effort and non-fatal; returns the count reconstructed. A
    duplicate is impossible — unjournaled_submitted_orders only returns ids
    with no trades row, so a successfully-journaled order is never re-created."""
    import journal
    db_path = getattr(ctx, "db_path", None)
    if not db_path:
        return 0
    pending = journal.unjournaled_submitted_orders(db_path)
    if not pending:
        return 0
    api = (ctx.get_alpaca_api() if hasattr(ctx, "get_alpaca_api")
           else getattr(ctx, "api", None))
    if api is None:
        return 0
    # Snapshot this profile's currently-journaled SHORT positions once, to
    # tell a long ENTRY (broker 'buy', no open short) from a buy-to-COVER
    # (broker 'buy' while a short is open — the reconciler's backfill_cover
    # owns that, NOT this recovery path).
    try:
        open_shorts = {
            (p.get("symbol") or "").upper()
            for p in journal.get_virtual_positions(db_path)
            # STOCK shorts only — an option leg on the same underlying carries
            # an occ_symbol and must not be mistaken for a stock short (the
            # 2026-06-25 occ-symbol-aware fix; an option-leg underlying landing
            # here would wrongly skip a lost-write stock reconstruction).
            if float(p.get("qty", 0) or 0) < 0 and not p.get("occ_symbol")}
    except Exception:
        open_shorts = set()
    # Reconstruct SHORT entries before BUYs: if BOTH a short-open and its cover
    # lost their journal writes (a two-lost-writes edge), rebuilding the short
    # first makes open_shorts see it so the cover-buy correctly SKIPS instead
    # of becoming a phantom long. We use this profile's OWN journaled shorts,
    # never the broker NET (the broker has no per-profile position on a shared
    # account, so a net read would be a cross-profile correctness violation).
    pending = sorted(
        pending,
        key=lambda r: 0 if str(r.get("side") or "").lower() == "sell" else 1)
    n = 0
    for rec in pending:
        oid = rec.get("order_id")
        broker_side = str(rec.get("side") or "").lower()
        intent = str(rec.get("intent") or "").lower()
        sym = rec.get("symbol")
        try:
            o = api.get_order(oid)
        except Exception as exc:
            logger.debug("reconstruct: get_order(%s) failed, will retry next "
                         "cycle: %s", oid, exc)
            continue  # broker unreachable for this order — retry next cycle
        status = str(getattr(o, "status", "") or "").lower()
        if status in ("canceled", "expired", "rejected"):
            journal.drop_submitted_order(db_path, oid)
            continue
        if status != "filled":
            continue  # still working — reconstruct once it fills
        try:
            filled_qty = float(getattr(o, "filled_qty", 0) or 0)
            fill_px = float(getattr(o, "filled_avg_price", 0) or 0)
        except (TypeError, ValueError) as exc:
            logger.debug("reconstruct: bad fill fields for %s: %s", oid, exc)
            continue
        if filled_qty <= 0 or fill_px <= 0:
            continue
        # Map the BROKER side ('buy'/'sell') to the JOURNAL side, reconstructing
        # ENTRIES only. A CLOSE (long sell / short cover) has an existing entry
        # row and is rebuilt by the reconciler's backfill_sell/backfill_cover
        # from the broker position change — never here. We must never rebuild a
        # close as an entry (that was the phantom-long bug: a buy-to-cover
        # reconstructed as a long, a short entry skipped entirely).
        if broker_side == "sell" and intent == "open_short":
            journal_side = "short"            # deliberate short ENTRY
        elif broker_side == "buy" and (sym or "").upper() not in open_shorts:
            journal_side = "buy"              # long ENTRY (no short to cover)
        else:
            continue  # long close, or buy-to-cover → reconciler's FIFO owns it
        journal.log_trade(
            symbol=sym, side=journal_side, qty=filled_qty, price=fill_px,
            order_id=oid, signal_type="AUTO_RECONCILE", status="open",
            fill_price=fill_px, occ_symbol=rec.get("occ_symbol"),
            reason="reconcile: reconstructed from broker fill — journal write "
                   "was lost at submit time",
            db_path=db_path)
        if journal_side == "short":
            # a later cover-buy for this symbol must now see the open short
            open_shorts.add((sym or "").upper())
        n += 1
    if n:
        logger.warning(
            "reconstructed %d unjournaled submitted order(s) for %s from "
            "broker truth (durable-journaling recovery)", n, db_path)
    return n


def reconcile_and_stamp(ctx, epoch: Optional[int] = None,
                        cross_profile_used_ids: Optional[set] = None) -> Dict[str, list]:
    """Reconcile one profile to broker truth, then stamp every symbol it
    touched as FRESH at `epoch` (default: the live cycle epoch).

    This is the ONE writer of the freshness ledger. Both the cycle-top
    reconcile (multi_scheduler) and the oversell door's just-in-time
    reconcile call it, so one successful reconcile makes every one of the
    profile's symbols actionable for the rest of the cycle.

    Reuses reconcile_with_ctx verbatim — no new reconcile logic on the
    load-bearing path. If reconcile raises, the stamp does NOT happen: the
    symbols stay stale and the door stays fail-closed. (2026-06-23,
    broker/journal divergence-class elimination.)"""
    import cycle_epoch
    import journal
    if epoch is None:
        epoch = cycle_epoch.current()
    # Durable-journaling recovery: recover any order the broker accepted but
    # whose journal write was lost, BEFORE reconciling, so it is owned and not
    # mistaken for an unowned orphan. Non-fatal. Then prune recovery rows that
    # are now safely journaled, to keep the ledger bounded.
    try:
        _reconstruct_unjournaled_submits(ctx)
        journal.prune_journaled_submitted_orders(getattr(ctx, "db_path", None))
    except Exception:
        logger.exception(
            "durable-journaling recovery failed (non-fatal) for %s",
            getattr(ctx, "db_path", None))
    result = reconcile_with_ctx(
        ctx, apply_changes=True,
        cross_profile_used_ids=cross_profile_used_ids)
    # A reconcile that did NOT reach broker truth must NOT stamp symbols fresh
    # — else the door treats a stale book as fresh and waves a naked sell
    # through (fail-OPEN). reconcile_with_ctx RETURNS (does not raise)
    # {"error": ...} when the broker is unreachable after retries, and
    # {"skipped": ...} for a profile with no broker account. Neither reconciled.
    if isinstance(result, dict) and result.get("error"):
        logger.error(
            "reconcile_and_stamp: reconcile FAILED for %s (%s) — NOT stamping "
            "fresh; the oversell door fails closed on this profile's sells "
            "until the broker is reachable again.",
            getattr(ctx, "db_path", None), result.get("error"))
        raise ReconcileUnavailable(str(result.get("error")))
    if isinstance(result, dict) and result.get("skipped"):
        # Archived/disabled profile with no broker account — there is no broker
        # reality to diverge from; nothing was reconciled, so do not stamp.
        return result
    db_path = getattr(ctx, "db_path", None)
    if db_path:
        try:
            import sqlite3
            from contextlib import closing
            with closing(sqlite3.connect(db_path)) as conn:
                syms = [r[0] for r in conn.execute(
                    "SELECT DISTINCT symbol FROM trades "
                    "WHERE symbol IS NOT NULL").fetchall()]
                # Option legs are keyed by their OCC symbol at the door
                # (kwargs["symbol"] is the OCC string), so stamp those too —
                # otherwise an option close would always read stale and the
                # door would re-reconcile every option order.
                try:
                    syms += [r[0] for r in conn.execute(
                        "SELECT DISTINCT occ_symbol FROM trades "
                        "WHERE occ_symbol IS NOT NULL").fetchall()]
                except sqlite3.OperationalError:
                    pass  # minimal-schema DB without occ_symbol
            if syms:
                journal.stamp_symbols_fresh(db_path, syms, epoch)
        except Exception:
            logger.exception(
                "reconcile_and_stamp: freshness stamp failed for %s; "
                "symbols stay stale (door will re-reconcile)", db_path)
    return result


def ensure_symbol_fresh(ctx, symbol: str, epoch: Optional[int] = None) -> None:
    """Guarantee THIS profile's journal for `symbol` is reconciled to broker
    truth at the live cycle epoch BEFORE a caller acts on it.

    Called by the oversell door on every stock sell. If the symbol is
    already fresh this cycle (the normal case — the cycle-top reconcile
    stamped it), this is a cheap ledger read and returns immediately. If
    stale, it runs a full reconcile_and_stamp — uncommon (a brand-new symbol
    not yet in the journal, or the first action on the symbol this cycle
    before the cycle-top reconcile reached it) and built on tested machinery.

    Fail-closed: if the reconcile raises (broker unreachable, etc.) the
    exception propagates so the door REFUSES the sell rather than act on a
    journal it could not freshen."""
    import cycle_epoch
    import journal
    if epoch is None:
        epoch = cycle_epoch.current()
    db_path = getattr(ctx, "db_path", None)
    if not db_path or not symbol:
        return
    # A ctx with no broker handle is a non-broker context (a unit-test double
    # or a degenerate ctx) — there is no broker reality to diverge from, so
    # the journal is the only truth and there is nothing to reconcile. Every
    # real production ctx (UserContext) exposes get_alpaca_api, so the gate
    # stays fully enforced live; this only spares contexts that physically
    # cannot reach a broker.
    if not (hasattr(ctx, "get_alpaca_api") or hasattr(ctx, "api")):
        return
    if journal.get_symbol_epoch(db_path, symbol) >= epoch:
        return  # already reconciled to broker truth this cycle
    # reconcile_and_stamp RAISES (ReconcileUnavailable) on broker-unreachable —
    # the door converts that to a refusal (fail-closed). A 'skipped' result
    # means the profile has no broker account to reconcile against (its journal
    # is its only truth), so don't refuse.
    result = reconcile_and_stamp(ctx, epoch=epoch)
    if isinstance(result, dict) and result.get("skipped"):
        return
    # The full-profile reconcile SUCCEEDED (it raises ReconcileUnavailable on
    # any broker error — so reaching here means the journal is now reconciled
    # to this profile's own broker truth). THIS symbol is therefore fresh too,
    # even a brand-new one the profile has never traded — e.g. a first-time
    # SHORT entry, which reconcile_and_stamp's "stamp every symbol in trades"
    # pass cannot cover because it isn't in trades yet. Stamp it explicitly so
    # the gate passes. (Without this the door refused EVERY first-time short —
    # the symbol stayed stale and the old re-check raised. 2026-06-24.)
    journal.stamp_symbols_fresh(db_path, [symbol], epoch)


def reconcile_with_ctx(ctx, apply_changes: bool = False,
                       cross_profile_used_ids: Optional[set] = None) -> Dict[str, list]:
    """Reconcile one profile from an already-built UserContext.

    `cross_profile_used_ids` is an optional set of order_ids already
    referenced by SELL/COVER rows in OTHER profiles' journals. The
    fallback match path uses it to avoid double-attributing one broker
    fill to multiple profiles. Callers running across all profiles
    should compute this once via `_all_journal_sell_order_ids` and
    pass it in.
    """
    name = ctx.display_name or f"profile_{getattr(ctx, 'profile_id', '?')}"
    api = ctx.get_alpaca_api() if hasattr(ctx, "get_alpaca_api") else ctx.api
    db_path = ctx.db_path

    # Disabled / archived profiles (no Alpaca account assigned) get
    # skipped silently — they're not "errors" in any meaningful sense.
    if not getattr(ctx, "alpaca_account_id", None):
        return {
            "skipped": "no alpaca_account_id (archived/disabled profile)",
            "cancel": [], "backfill_sell": [], "backfill_cover": [],
            "backfill_partial_sell": [], "fix_partial_entry": [],
            "ambiguous": [], "orphan_close": [],
            "option_orphan_close": [], "real_held": 0,
            "profile": name,
            "profile_id": getattr(ctx, "profile_id", None),
        }

    actions = {
        "cancel": [],
        "backfill_sell": [],   # long full close
        "backfill_cover": [],  # short full close
        "backfill_partial_sell": [],  # long partial close
        "fix_partial_entry": [],      # update journal qty/price to actual fill
        "uncancel_sell": [],          # phantom SELL (broker fully canceled): undo
        "fix_partial_sell": [],       # partial-fill SELL: adjust journal qty
        "ambiguous": [],
        # A3 (2026-06-16): broker-flat position with NO own order_id
        # explaining the close. Halts (never fuzzy-claims a sibling's
        # fill, never silently diverges). See PROFILE_ORDER_ISOLATION.md.
        "orphan_close": [],
        # 2026-06-17: option legs the broker no longer holds, auto-closed
        # by the option-orphan backstop. EXPECTED reconciles — NOT a halt.
        "option_orphan_close": [],
        "real_held": 0,
    }

    # PHANTOM-SELL DETECTION (runs first so subsequent open-row sweep
    # sees the corrected state). For each closed SELL/COVER row, verify
    # the broker order actually filled. If the broker order is
    # canceled/expired/rejected with filled_qty=0, the journal row is
    # phantom — the SELL was logged on submit but never executed.
    # Caught 2026-05-06 (profile_6 #83 B 27): trade_pipeline marks
    # SELL status='closed' immediately on submit, doesn't wait for
    # fill confirmation. If Alpaca cancels the order (wash trade,
    # off-hours, etc.) the journal claims a SELL that never happened
    # → broker_orphan in the aggregate audit.
    #
    # 2026-05-07: trade_pipeline / trader / options_roll_manager now
    # write status='pending_fill' until _task_update_fills confirms
    # via filled_avg_price. Reconcile checks BOTH ('closed' for legacy
    # rows + 'pending_fill' for new rows) so phantom detection still
    # works during the rollover window.
    try:
        import sqlite3 as _sqlite3
        sell_conn = _sqlite3.connect(db_path)
        sell_rows = sell_conn.execute(
            "SELECT id, symbol, side, qty, order_id "
            "FROM trades WHERE side IN ('sell', 'cover') "
            "AND status IN ('closed', 'pending_fill') "
            "AND order_id IS NOT NULL"
        ).fetchall()
        sell_conn.close()
    except Exception:
        sell_rows = []
    for tid, sym, side, qty, oid in sell_rows:
        order, _exc = _retrying_call(api.get_order, oid)
        if order is None:
            continue
        try:
            broker_filled = float(getattr(order, "filled_qty", 0) or 0)
        except (ValueError, TypeError, AttributeError) as _bq_exc:
            # Per-row broker filled-qty parse loop; skip rows with
            # malformed broker response. Surface for follow-up.
            logger.debug(
                "broker filled-qty parse failed: %s: %s",
                type(_bq_exc).__name__, _bq_exc,
            )
            continue
        broker_status = getattr(order, "status", "")
        journal_qty = float(qty or 0)
        if broker_status in ("canceled", "expired", "rejected") and broker_filled == 0:
            # Full phantom — undo
            actions["uncancel_sell"].append({
                "trade_id": tid, "symbol": sym, "side": side, "qty": qty,
                "order_id": oid, "broker_status": broker_status,
            })
        elif (broker_status in ("canceled", "expired", "rejected")
              and 0 < broker_filled < journal_qty - 0.001):
            # Partial fill — adjust journal qty to actual broker fill
            try:
                fap = float(getattr(order, "filled_avg_price", 0) or 0)
            except Exception:
                fap = 0
            actions["fix_partial_sell"].append({
                "trade_id": tid, "symbol": sym, "side": side,
                "journal_qty": journal_qty,
                "broker_filled_qty": broker_filled,
                "broker_avg_fill_price": fap,
                "order_id": oid, "broker_status": broker_status,
            })

    positions, exc = _retrying_call(api.list_positions)
    if positions is None:
        return {"error": f"failed to fetch positions after retries: {exc}", **actions}

    conn = _open_journal_conn(db_path)
    try:
        # OPTION-ORPHAN BACKSTOP (2026-06-17) — close any option leg the
        # broker no longer holds (LONG or SHORT), before the stock loop.
        # The stock loop below skips option rows (now owned by this
        # pass); the pass covers short legs the loop's `side=='sell'`
        # skip dropped entirely (O8). See reconcile_option_orphans.
        _opt_orphans = reconcile_option_orphans(
            api, conn, positions,
            today=datetime.now(timezone.utc).date(),
            apply_changes=apply_changes,
        )
        actions["option_orphan_close"] = [
            d for d in _opt_orphans if d.get("kind") == "auto_closed"]
        # A never-filled option entry is a plain cancel — surface it in
        # the existing cancel bucket (the backstop already wrote the
        # status; this is for the summary/idempotent apply).
        for d in _opt_orphans:
            if d.get("kind") == "canceled":
                actions["cancel"].append({
                    "trade_id": d["trade_id"], "symbol": d["symbol"],
                    "qty": d["qty"], "side": d["side"],
                    "entry_status": "canceled",
                })

        rows = _select_open_rows(conn)

        # Seed the dedup set with order_ids already attributed by sibling
        # profiles so the fallback match path doesn't double-attribute one
        # broker fill to multiple profiles.
        used_sell_ids: set = set(cross_profile_used_ids or set())
        used_cover_ids: set = set(cross_profile_used_ids or set())

        for r in rows:
            # Option legs are owned by reconcile_option_orphans (above)
            # + the expiry sweep + fill-confirmation — skip them here so
            # the stock loop's account-level-qty logic never touches an
            # option leg (and never halts a held option). Safe accessor:
            # minimal-schema fixtures may omit occ_symbol.
            try:
                _row_occ = r["occ_symbol"]
            except (KeyError, IndexError):
                _row_occ = None
            if _row_occ:
                continue
            # For options: look up by OCC symbol; for stocks: by underlying.
            broker_lookup_sym = _lookup_symbol_for_row(r)
            sym = (r["symbol"] or "").upper()
            side = (r["side"] or "").lower()
            qty = float(r["qty"] or 0)

            # Determine if this is a long-open or short-open.
            # side='buy' → long open; side='short' → short open (P1.10).
            # side='sell' open is handled by the existing reconcile pass.
            if side == "sell":
                continue
            is_short = (side == "short")
            broker_qty = _broker_qty_for(positions, broker_lookup_sym)

            # PARTIAL ENTRY FILL — independent of current broker state.
            # If the entry order status is canceled/expired/rejected with
            # filled_qty>0, correct the journal qty to actual filled, and
            # leave status='open' so next pass re-evaluates.
            order_id = r["order_id"]
            if order_id:
                entry_order, exc = _retrying_call(api.get_order, order_id)
                if entry_order is not None:
                    entry_status = getattr(entry_order, "status", "?")
                    try:
                        entry_filled = float(getattr(entry_order, "filled_qty", 0) or 0)
                    except Exception:
                        entry_filled = 0
                    if (entry_status in ("canceled", "expired", "rejected")
                            and 0 < entry_filled < qty - 0.001):
                        actions["fix_partial_entry"].append({
                            "trade_id": r["id"], "symbol": sym, "side": side,
                            "original_qty": qty,
                            "actual_filled_qty": entry_filled,
                            "entry_avg_fill_price": float(
                                getattr(entry_order, "filled_avg_price", 0) or 0,
                            ),
                            "order_id": order_id, "entry_status": entry_status,
                        })
                        continue

            # PROTECTIVE-ORDER FILL CHECK — the per-profile correctness gate.
            # Always look up THIS profile's protective stop/TP/trailing
            # orders, regardless of the symbol's account-level broker_qty.
            # Sibling profiles holding shares of the same symbol previously
            # masked this profile's protective fills. (Caught 2026-05-06:
            # profile_9 trailing-stop fired GT 573, broker still showed
            # 1399 GT from siblings → reconcile said "real_held" and missed
            # the exit.) This is the multi-profile-correct backfill path.
            prot_kind, prot_detail = _detect_protective_fill(
                api, r, used_sell_ids,
            )
            # 2026-06-11 — LIVE own-journal check (same race as the
            # phantom path below): an exit journaled mid-pass is
            # invisible to the task-start dedup snapshot. If a
            # non-placeholder row already carries this order_id,
            # the fill is journaled — processing it again would
            # double-count (the bracket-child exemption would even
            # INSERT a duplicate exit row). Placeholder rows
            # (pending_protective) keep flowing through the
            # established pending-UPDATE path below.
            if prot_kind in ("backfill_full", "backfill_partial"):
                _fresh_exit = conn.execute(
                    "SELECT 1 FROM trades WHERE order_id = ? AND "
                    "COALESCE(status, '') != 'pending_protective' "
                    "LIMIT 1",
                    (prot_detail["order_id"],),
                ).fetchone()
                if _fresh_exit:
                    logger.info(
                        "Reconcile: protective/exit fill %s for %s "
                        "already journaled in this profile (landed "
                        "after the dedup snapshot) — skipping.",
                        str(prot_detail["order_id"])[:8], sym,
                    )
                    continue
            if prot_kind == "backfill_full":
                used_sell_ids.add(prot_detail["order_id"])
                entry_price = float(r["price"] or 0)
                # 2026-05-21 — tag entries from the PROTECTIVE path
                # (where the order_id was tracked on the entry row via
                # protective_*_order_id) so the safety-net halt counter
                # only counts truly-orphan synthesis actions. A
                # protective fill IS expected synthesis: we placed the
                # protective order via submit_order (journaled then),
                # but the FILL happens broker-side on its own with no
                # corresponding code-path call — only the reconciler
                # can see it. That's the reconciler's design, not an
                # orphan-fill bug. The memory rule "no orphan broker
                # fills" is about submit_order journaling leaks, not
                # about broker-side protective triggers we configured.
                if is_short:
                    actions["backfill_cover"].append({
                        "trade_id": r["id"], "symbol": sym, "qty": qty,
                        "short_price": entry_price,
                        "cover_order_id": prot_detail["order_id"],
                        "cover_price": prot_detail["filled_avg_price"],
                        "cover_qty": prot_detail["filled_qty"],
                        "cover_filled_at": prot_detail["filled_at"].isoformat(),
                        "cover_order_type": prot_detail["order_type"],
                        "source": "protective",
                    })
                else:
                    actions["backfill_sell"].append({
                        "trade_id": r["id"], "symbol": sym, "qty": qty,
                        "buy_price": entry_price,
                        "sell_order_id": prot_detail["order_id"],
                        "sell_price": prot_detail["filled_avg_price"],
                        "sell_qty": prot_detail["filled_qty"],
                        "sell_filled_at": prot_detail["filled_at"].isoformat(),
                        "sell_order_type": prot_detail["order_type"],
                        "source": "protective",
                    })
                continue
            if prot_kind == "backfill_partial":
                used_sell_ids.add(prot_detail["order_id"])
                actions["backfill_partial_sell"].append({
                    "trade_id": r["id"], "symbol": sym,
                    "journal_qty": qty, "broker_qty": broker_qty,
                    "buy_price": float(r["price"] or 0),
                    "sell_order_id": prot_detail["order_id"],
                    "sell_price": prot_detail["filled_avg_price"],
                    "sell_qty": prot_detail["filled_qty"],
                    "sell_filled_at": prot_detail["filled_at"].isoformat(),
                    "sell_order_type": prot_detail["order_type"],
                    "source": "protective",
                })
                continue

            # Normalize: for shorts, "real_held" means broker_qty < 0
            if is_short:
                real_held = broker_qty < -0.001
            else:
                real_held = broker_qty > 0.001

            if real_held:
                actions["real_held"] += 1
                continue

            # Phantom — full close
            if is_short:
                kind, detail = _classify_short_phantom(
                    api, r, broker_qty, used_cover_ids,
                )
            else:
                kind, detail = _classify_long_phantom(
                    api, r, broker_qty, used_sell_ids,
                )

            if kind == "cancel":
                actions["cancel"].append({
                    "trade_id": r["id"], "symbol": sym, "qty": qty,
                    "side": side, **detail,
                })
            elif kind == "partial_entry":
                actions["fix_partial_entry"].append({
                    "trade_id": r["id"], "symbol": sym,
                    "side": side,
                    "original_qty": qty,
                    **detail,
                })
            elif kind == "orphan_close":
                # A3 PROFILE ISOLATION (2026-06-16) — the position is
                # gone at the broker but NO order_id in THIS profile's
                # own journal explains the close. The fuzzy
                # symbol/qty/time matcher that used to "explain" these
                # was DELETED because on a shared account it attributed
                # siblings' fills to this profile. An unexplained close
                # is a genuine divergence (external/manual close, a
                # legacy NULL protective id, or — pre-A1/A2 — a sibling
                # that touched our order). HALT for operator review;
                # never silently leave it diverged and never fuzzy-
                # claim a sibling's fill. See PROFILE_ORDER_ISOLATION.md.
                #
                # LIVE own-journal re-check (CPNG 93ecef03 race class):
                # the open-rows set was snapshotted at task start. If
                # the fill-confirmation state machine journaled the
                # exit and flipped THIS entry row to a terminal status
                # mid-pass, the "phantom" is stale — skip it so we
                # don't false-HALT a position that just closed cleanly.
                _oc_oid = r["order_id"]
                _live_status = conn.execute(
                    "SELECT 1 FROM trades WHERE order_id = ? "
                    "AND COALESCE(status,'open') = 'open' LIMIT 1",
                    (_oc_oid,),
                ).fetchone() if _oc_oid else None
                if _oc_oid and not _live_status:
                    logger.info(
                        "Reconcile: %s entry %s no longer open in the "
                        "live journal (closed after the snapshot) — "
                        "not an orphan, skipping.",
                        sym, str(_oc_oid)[:8],
                    )
                    continue
                actions["orphan_close"].append({
                    "trade_id": r["id"], "symbol": sym, "qty": qty,
                    "side": side, **detail,
                })
            elif kind == "ambiguous":
                actions["ambiguous"].append({
                    "trade_id": r["id"], "symbol": sym, "qty": qty,
                    "side": side, **detail,
                })

        if apply_changes:
            # Phantom-SELL undo first: mark the offending SELL/COVER row as
            # 'canceled' AND reopen the matching closed BUY/SHORT so the
            # position is correctly reflected as still held. Match by qty +
            # symbol + side, picking the most recent closed entry.
            for a in actions["fix_partial_sell"]:
                # Partial-fill SELL: journal claimed qty=N, broker only
                # filled M (M < N). Adjust journal qty to M and recompute
                # pnl. The (N - M) shares are still at the broker — the
                # next reconcile pass will see the matching closed BUY
                # has more open qty than journal SELL covers and flag the
                # remainder for re-handling.
                new_qty = a["broker_filled_qty"]
                new_price = a["broker_avg_fill_price"]
                # Look up the matching BUY/SHORT to recompute pnl
                opener_side = "short" if a["side"] == "cover" else "buy"
                opener = conn.execute(
                    "SELECT price FROM trades WHERE symbol=? AND side=? "
                    "ORDER BY timestamp DESC LIMIT 1",
                    (a["symbol"], opener_side),
                ).fetchone()
                buy_price = float(opener[0]) if opener and opener[0] else 0
                if a["side"] == "cover":
                    new_pnl = round((buy_price - new_price) * new_qty, 2)
                else:
                    new_pnl = round((new_price - buy_price) * new_qty, 2)
                conn.execute(
                    "UPDATE trades SET qty=?, price=?, fill_price=?, pnl=?, "
                    "reason=COALESCE(reason || ' | ', '') || ? "
                    "WHERE id=?",
                    (new_qty, new_price, new_price, new_pnl,
                     f"reconcile: broker only filled {new_qty:.0f} of "
                     f"{a['journal_qty']:.0f} ({a['broker_status']}); "
                     f"qty corrected", a["trade_id"]),
                )
                # 2026-06-11 — REOPEN the matching entry. The SELL
                # pipeline flips the entry to 'closed' at SUBMIT time
                # (position_closed = sell_qty >= held_qty); when the
                # broker then only fills part of the sell, the entry
                # must come back so the FIFO book shows the remainder
                # (entry_qty − filled_qty). Without this, the unsold
                # shares vanish from the virtual book while staying
                # at the broker — p97 lost $24.6K of book value to
                # exactly this (PLUG 4,347 / SMCI 311 / NU 177 /
                # IONZ 319 orphaned shares). The protective sweep
                # re-protects the reopened remainder next cycle.
                _reopen_reason = (
                    f"reconcile: reopened — exit only filled "
                    f"{new_qty:.0f} of {a['journal_qty']:.0f}, "
                    f"remainder still held"
                )
                try:
                    _reopened = conn.execute(
                        "UPDATE trades SET status='open', "
                        "reason=COALESCE(reason || ' | ', '') || ? "
                        "WHERE id = ("
                        "  SELECT id FROM trades WHERE symbol=? AND side=? "
                        "  AND status='closed' AND occ_symbol IS NULL "
                        "  ORDER BY timestamp DESC, id DESC LIMIT 1"
                        ")",
                        (_reopen_reason, a["symbol"], opener_side),
                    ).rowcount
                except sqlite3.OperationalError:
                    # Minimal-schema fixtures without occ_symbol.
                    _reopened = conn.execute(
                        "UPDATE trades SET status='open', "
                        "reason=COALESCE(reason || ' | ', '') || ? "
                        "WHERE id = ("
                        "  SELECT id FROM trades WHERE symbol=? AND side=? "
                        "  AND status='closed' "
                        "  ORDER BY timestamp DESC, id DESC LIMIT 1"
                        ")",
                        (_reopen_reason, a["symbol"], opener_side),
                    ).rowcount
                if _reopened:
                    logger.info(
                        "Reconcile: reopened %s %s entry — partial "
                        "exit (%.0f of %.0f filled), remainder back "
                        "on the book.",
                        a["symbol"], opener_side, new_qty,
                        a["journal_qty"],
                    )
            for a in actions["uncancel_sell"]:
                conn.execute(
                    "UPDATE trades SET status='canceled', pnl=NULL, "
                    "reason=COALESCE(reason || ' | ', '') || ? "
                    "WHERE id=?",
                    (f"reconcile: broker order {a['order_id'][:8]} "
                     f"never filled ({a['broker_status']})", a["trade_id"]),
                )
                opener_side = "short" if a["side"] == "cover" else "buy"
                opener = conn.execute(
                    "SELECT id FROM trades WHERE symbol=? AND side=? "
                    "AND status='closed' AND qty=? "
                    "ORDER BY timestamp DESC LIMIT 1",
                    (a["symbol"], opener_side, a["qty"]),
                ).fetchone()
                if opener:
                    conn.execute(
                        "UPDATE trades SET status='open', pnl=NULL, "
                        "reason=COALESCE(reason || ' | ', '') || ? "
                        "WHERE id=?",
                        (f"reconcile: reopened after phantom "
                         f"{a['side'].upper()} undone", opener[0]),
                    )
            for a in actions["cancel"]:
                # pnl=NULL: a canceled trade realized nothing. Leaving a
                # speculative pnl on a canceled row inflates realized
                # P&L (the p121 −5,985 decomposition gap). Invariant:
                # canceled/expired/rejected rows carry no pnl. 2026-06-16.
                conn.execute(
                    "UPDATE trades SET status='canceled', pnl=NULL WHERE id=?",
                    (a["trade_id"],),
                )
            for a in actions["fix_partial_entry"]:
                # Update qty + price to the broker's actual fill, leave
                # status='open'. Next reconcile pass will re-evaluate against
                # broker truth (which now sees the new qty).
                conn.execute(
                    "UPDATE trades SET qty=?, price=?, fill_price=?, "
                    "reason=COALESCE(reason || ' | ', '') || ? "
                    "WHERE id=?",
                    (a["actual_filled_qty"], a["entry_avg_fill_price"],
                     a["entry_avg_fill_price"],
                     f"reconcile: corrected partial-fill (was qty={a['original_qty']})",
                     a["trade_id"]),
                )
            # 2026-05-21 — Protective-source path: UPDATE pre-journaled
            # `pending_protective` row, no synthesis. This is the
            # primary path for protective fills going forward: every
            # bracket_orders.submit_protective_* call writes a
            # `pending_protective` trades row at PLACEMENT time, so
            # when the broker fills it the reconciler just flips that
            # row to status='closed' with fill data. No INSERT needed.
            #
            # If the pending row is missing (legacy entry from before
            # this refactor landed), the protective-source entry stays
            # in the synthesis bucket and trips the halt — same as a
            # true orphan. Fixed via one-time backfill scripts for
            # known legacy gaps (scripts/backfill_protective_orphan_*).
            applied_protective = 0
            still_orphan_protective = []
            for bucket_name, kind in (
                ("backfill_sell", "sell"),
                ("backfill_cover", "cover"),
                ("backfill_partial_sell", "partial"),
            ):
                for a in actions[bucket_name]:
                    if a.get("source") != "protective":
                        continue
                    fill_oid = (a.get("sell_order_id")
                                or a.get("cover_order_id"))
                    if not fill_oid:
                        continue
                    pending_row = conn.execute(
                        "SELECT id, qty FROM trades "
                        "WHERE order_id = ? AND status = 'pending_protective' "
                        "LIMIT 1",
                        (fill_oid,),
                    ).fetchone()
                    if not pending_row:
                        # 2026-06-10 (PM) — bracket children are
                        # broker-CREATED OCO legs; no submit_order
                        # call of ours ever placed them, so "no
                        # pending row" is the architecture, not a
                        # journaling leak. The at-submit stamp +
                        # pending-row write can race the broker's
                        # child materialization (lost on every entry
                        # of the first post-reset session → all 13
                        # profiles falsely HALTED on the first child
                        # fill, WCT 3f61e6fe). If the fill order is a
                        # child leg of this entry's bracket parent,
                        # it's expected protective synthesis — exempt
                        # from the halt counter; the end-of-pass
                        # journal sync backfills the row.
                        if _is_bracket_child_fill(
                            api, conn, a, fill_oid,
                        ):
                            # PERFORM the expected synthesis inline —
                            # the equivalent of the pending-row UPDATE
                            # path, minus the pre-existing row: write
                            # the closed exit row from broker fill
                            # data and close the entry. Without this
                            # the exemption only suppresses the halt
                            # and the entry stays open forever
                            # (caught live: WCT entry still open
                            # after the first exempted pass — broker
                            # flat, journal long).
                            _side = ("cover" if kind == "cover"
                                     else "sell")
                            _price = (a.get("sell_price")
                                      or a.get("cover_price"))
                            _fqty = (a.get("sell_qty")
                                     or a.get("cover_qty"))
                            _fat = (a.get("sell_filled_at")
                                    or a.get("cover_filled_at"))
                            conn.execute(
                                "INSERT INTO trades "
                                "(timestamp, symbol, side, qty, "
                                " price, fill_price, order_id, "
                                " signal_type, strategy, status, "
                                " reason) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, "
                                "        'closed', ?)",
                                (
                                    _fat, a.get("symbol"), _side,
                                    _fqty, _price, _price, fill_oid,
                                    "PROTECTIVE_FILL",
                                    "reconcile_backfill_bracket",
                                    "bracket child fill confirmed "
                                    f"@ ${float(_price or 0):.2f} "
                                    "(reconciler; no pending row — "
                                    "at-submit journaling raced "
                                    "child materialization)",
                                ),
                            )
                            if kind != "partial":
                                conn.execute(
                                    "UPDATE trades SET status='closed' "
                                    "WHERE id=?",
                                    (a["trade_id"],),
                                )
                            applied_protective += 1
                            logger.info(
                                "Reconciler: %s fill %s is a bracket "
                                "child of its entry's parent order — "
                                "expected protective synthesis "
                                "applied (exit row written, entry "
                                "closed), no halt.",
                                a.get("symbol"), fill_oid[:8],
                            )
                            continue
                        # No pre-journaled row → legacy gap → leave in
                        # the synthesis bucket for halt + alert
                        still_orphan_protective.append(a)
                        continue
                    fill_price = (a.get("sell_price")
                                   or a.get("cover_price"))
                    filled_at = (a.get("sell_filled_at")
                                  or a.get("cover_filled_at"))
                    filled_qty = (a.get("sell_qty")
                                   or a.get("cover_qty"))
                    # UPDATE the pending row to closed-with-fill-data.
                    # Partial path: leave qty at the journal value;
                    # add a NOTE so a future reconcile cycle can
                    # resume tracking the remainder.
                    note = ""
                    if kind == "partial":
                        note = (
                            f" | reconciler: partial fill "
                            f"{filled_qty}/{a.get('journal_qty')}"
                        )
                    conn.execute(
                        "UPDATE trades "
                        "SET status='closed', price=?, fill_price=?, "
                        "    qty=?, "
                        "    reason=COALESCE(reason || ' | ', '') || ? "
                        "WHERE id=?",
                        (
                            fill_price, fill_price, filled_qty,
                            f"reconciler: protective fill confirmed "
                            f"@ ${fill_price:.2f}{note}",
                            pending_row[0],
                        ),
                    )
                    # Mark the entry-side BUY/SHORT closed too —
                    # the protective fully exited the position.
                    if kind != "partial":
                        conn.execute(
                            "UPDATE trades SET status='closed' WHERE id=?",
                            (a["trade_id"],),
                        )
                    applied_protective += 1
            if applied_protective:
                logger.info(
                    "Reconciler: applied %d protective fill update(s) "
                    "(no synthesis, no halt)",
                    applied_protective,
                )

            # 2026-05-19 reconciler safety net: synthesis paths
            # (backfill_sell / backfill_cover / backfill_partial_sell)
            # used to silently INSERT a new SELL/COVER row reflecting
            # what the broker says happened. Per
            # `feedback_no_orphan_broker_fills`, this papers over a
            # real bug — every broker order MUST be journaled by the
            # submit_order code path. If we got here, the journaling
            # in that code path failed for some submit_order leak.
            # Instead of synthesizing, HALT the profile so trading
            # stops on the new-entry side until the leak is found.
            # Auto-clears next pass when synthesis no longer needed.
            #
            # 2026-05-21 — refined: only count PHANTOM-source entries
            # (or PROTECTIVE entries whose pending row is missing —
            # legacy gap) toward the halt. The vast majority of
            # protective fills now hit the UPDATE path above and
            # never touch the halt counter at all.
            def _halt_count(rows):
                return sum(
                    1 for a in rows
                    if (a.get("source") != "protective"
                        or a in still_orphan_protective)
                )
            synthesis_actions = (
                _halt_count(actions["backfill_sell"])
                + _halt_count(actions["backfill_cover"])
                + _halt_count(actions["backfill_partial_sell"])
                # A3 (2026-06-16): an unexplained broker-flat close is
                # a divergence the operator must see — count it toward
                # the halt so the position can't silently rot.
                + len(actions["orphan_close"])
            )
            if synthesis_actions:
                from halt_helpers import halt_and_alert
                detail_lines = []
                for a in actions["backfill_sell"]:
                    detail_lines.append(
                        f"  backfill_sell: {a['symbol']} qty={a['sell_qty']} "
                        f"sell_order={a['sell_order_id'][:8]} "
                        f"@ ${a['sell_price']:.2f} "
                        f"({a.get('sell_order_type', '?')})"
                    )
                for a in actions["backfill_cover"]:
                    detail_lines.append(
                        f"  backfill_cover: {a['symbol']} qty={a['cover_qty']} "
                        f"cover_order={a['cover_order_id'][:8]} "
                        f"@ ${a['cover_price']:.2f} "
                        f"({a.get('cover_order_type', '?')})"
                    )
                for a in actions["backfill_partial_sell"]:
                    detail_lines.append(
                        f"  backfill_partial_sell: {a['symbol']} "
                        f"qty={a['sell_qty']}/{a['journal_qty']} "
                        f"sell_order={a['sell_order_id'][:8]}"
                    )
                for a in actions["orphan_close"]:
                    detail_lines.append(
                        f"  orphan_close: {a['symbol']} qty={a['qty']} "
                        f"({a['side']}) — broker flat, no OWN order_id "
                        f"explains the close"
                    )
                pid = getattr(ctx, "profile_id", None)
                if pid is not None:
                    title = (
                        f"Reconciler safety net: {synthesis_actions} "
                        f"synthesis action(s) needed — profile HALTED"
                    )
                    detail = (
                        "The reconciler detected broker fill(s) that "
                        "would have required SYNTHESIZING journal rows. "
                        "Per the atomic-journaling contract, this "
                        "indicates a submit_order code path failed to "
                        "journal in-line. Profile is HALTED until the "
                        "next reconcile pass shows no synthesis needed "
                        "(auto-clear) OR until the operator clears "
                        "manually after fixing the leak.\n\n"
                        + "\n".join(detail_lines)
                    )
                    halt_and_alert(
                        profile_id=pid, db_path=db_path,
                        alert_type="reconciler_synthesis_halt",
                        title=title, detail=detail,
                    )
                # Record the not-performed actions on the result so the
                # CLI summary surfaces them as "would have backfilled".
                actions["halted_synthesis_count"] = synthesis_actions
            else:
                # No synthesis needed this pass — auto-clear any halt
                # that was set on a previous pass.
                pid = getattr(ctx, "profile_id", None)
                if pid is not None:
                    try:
                        from halt_helpers import is_halted, clear_halt
                        halted, _reason = is_halted(pid)
                        if halted and _reason and _reason.startswith(
                            "Reconciler safety net:"
                        ):
                            clear_halt(pid, source="reconciler_auto_clear")
                    except Exception as _hc_exc:
                        logger.warning(
                            "halt auto-clear check failed for pid=%s: %s: %s",
                            pid, type(_hc_exc).__name__, _hc_exc,
                        )
            conn.commit()

    finally:
        conn.close()

    # Run the existing FIFO P&L backfill so SELL rows get pnl computed.
    has_writes = (actions["cancel"] or actions["backfill_sell"]
                  or actions["backfill_cover"]
                  or actions["backfill_partial_sell"]
                  or actions["fix_partial_entry"])
    if apply_changes and has_writes:
        from journal import reconcile_trade_statuses
        broker_open_symbols = {
            (p.symbol or "").upper() for p in positions
            if float(getattr(p, "qty", 0) or 0) != 0
        }
        reconcile_trade_statuses(db_path=db_path, open_symbols=broker_open_symbols)

    actions["profile"] = name
    actions["profile_id"] = getattr(ctx, "profile_id", None)
    return actions


def reconcile_profile(profile_id: int, apply_changes: bool = False,
                      cross_profile_used_ids: Optional[set] = None) -> Dict[str, list]:
    """CLI-style: build the ctx from profile_id, then delegate."""
    from models import build_user_context_from_profile
    ctx = build_user_context_from_profile(profile_id)
    return reconcile_with_ctx(ctx, apply_changes=apply_changes,
                              cross_profile_used_ids=cross_profile_used_ids)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="actually write changes (default: dry-run)")
    ap.add_argument("--profile", type=int, default=None,
                    help="run for a single profile id (default: all active)")
    ap.add_argument("--quiet", action="store_true",
                    help="cron-friendly: print only summary + errors")
    args = ap.parse_args()

    from models import get_active_profile_ids
    profile_ids = [args.profile] if args.profile else get_active_profile_ids()

    # Pre-compute the cross-profile dedup set so the fallback match
    # path can't double-attribute one broker fill to multiple profiles.
    cross_used = _all_journal_sell_order_ids(get_active_profile_ids())

    grand = {"cancel": 0, "backfill_sell": 0, "backfill_cover": 0,
             "backfill_partial_sell": 0, "fix_partial_entry": 0,
             "ambiguous": 0, "orphan_close": 0, "option_orphan_close": 0,
             "real_held": 0, "errored": 0}

    if not args.quiet:
        print(f"=== Reconcile {'APPLY' if args.apply else 'DRY-RUN'} ===\n")

    for p_id in profile_ids:
        try:
            res = reconcile_profile(p_id, apply_changes=args.apply,
                                    cross_profile_used_ids=cross_used)
        except Exception as e:
            print(f"profile_{p_id}: ERROR {e}")
            grand["errored"] += 1
            continue
        if "skipped" in res:
            if not args.quiet:
                print(f"p{p_id:>2} {res['profile'][:30]:<30s}  skipped: {res['skipped']}")
            continue
        if "error" in res:
            print(f"profile_{p_id} ({res.get('profile')}): ERROR {res['error']}")
            grand["errored"] += 1
            continue

        n_c = len(res["cancel"])
        n_bs = len(res["backfill_sell"])
        n_bc = len(res["backfill_cover"])
        n_bps = len(res["backfill_partial_sell"])
        n_fp = len(res["fix_partial_entry"])
        n_a = len(res["ambiguous"])
        n_oc = len(res.get("orphan_close", []))
        n_ooc = len(res.get("option_orphan_close", []))
        n_r = res["real_held"]

        if not args.quiet or (n_c + n_bs + n_bc + n_bps + n_fp + n_a + n_oc + n_ooc) > 0:
            print(f"p{p_id:>2} {res['profile'][:30]:<30s}  "
                  f"real={n_r:>3}  cancel={n_c:>2}  bs={n_bs:>2}  "
                  f"bc={n_bc:>2}  bps={n_bps:>2}  fp={n_fp:>2}  amb={n_a:>2}  "
                  f"orphan={n_oc:>2}  opt_orphan={n_ooc:>2}")
        if not args.quiet:
            for a in res["cancel"]:
                print(f"     CANCEL    #{a['trade_id']:<4} {a['symbol']:>5} {a.get('side',''):>5} "
                      f"qty={a['qty']:>6.0f}  entry_status={a['entry_status']}")
            for a in res["backfill_sell"]:
                pnl = (a["sell_price"] - a["buy_price"]) * a["qty"]
                sign = "+" if pnl >= 0 else ""
                print(f"     SELL      #{a['trade_id']:<4} {a['symbol']:>5} qty={a['qty']:>6.0f}  "
                      f"buy=${a['buy_price']:>7.2f} sell=${a['sell_price']:>7.2f}  "
                      f"realized={sign}${pnl:>9.2f}  ({a['sell_order_type']})")
            for a in res["backfill_cover"]:
                pnl = (a["short_price"] - a["cover_price"]) * a["qty"]
                sign = "+" if pnl >= 0 else ""
                print(f"     COVER     #{a['trade_id']:<4} {a['symbol']:>5} qty={a['qty']:>6.0f}  "
                      f"short=${a['short_price']:>7.2f} cover=${a['cover_price']:>7.2f}  "
                      f"realized={sign}${pnl:>9.2f}  ({a['cover_order_type']})")
            for a in res["backfill_partial_sell"]:
                pnl = (a["sell_price"] - a["buy_price"]) * a["sell_qty"]
                sign = "+" if pnl >= 0 else ""
                print(f"     PARTIAL   #{a['trade_id']:<4} {a['symbol']:>5} "
                      f"journal={a['journal_qty']:>5.0f} broker={a['broker_qty']:>5.0f}  "
                      f"sold={a['sell_qty']:>5.0f} @ ${a['sell_price']:>7.2f}  "
                      f"realized={sign}${pnl:>9.2f}")
            for a in res["fix_partial_entry"]:
                print(f"     FIX_QTY   #{a['trade_id']:<4} {a['symbol']:>5} "
                      f"was qty={a['original_qty']:>5.0f}  "
                      f"actual={a['actual_filled_qty']:>5.0f} @ ${a['entry_avg_fill_price']:>7.2f}")
            for a in res["ambiguous"]:
                print(f"     AMBIGUOUS #{a['trade_id']:<4} {a['symbol']:>5} {a.get('side',''):>5} "
                      f"qty={a['qty']:>6.0f}  reason: {a['reason']}")
            for a in res.get("orphan_close", []):
                print(f"     ORPHAN    #{a['trade_id']:<4} {a['symbol']:>5} {a.get('side',''):>5} "
                      f"qty={a['qty']:>6.0f}  reason: {a['reason']}")

        grand["cancel"] += n_c
        grand["backfill_sell"] += n_bs
        grand["backfill_cover"] += n_bc
        grand["backfill_partial_sell"] += n_bps
        grand["fix_partial_entry"] += n_fp
        grand["ambiguous"] += n_a
        grand["orphan_close"] += n_oc
        grand["option_orphan_close"] += n_ooc
        grand["real_held"] += n_r

    print(f"\n=== TOTALS ===")
    for k, v in grand.items():
        print(f"  {k:<24s}: {v:>3}")
    if not args.apply:
        print(f"\nDry-run only. Re-run with --apply to write changes.")
    if grand["ambiguous"] > 0 or grand["orphan_close"] > 0 or grand["errored"] > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
