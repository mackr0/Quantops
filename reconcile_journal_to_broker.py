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


def _find_matching_exit_fill(api, symbol: str, qty: float, after_ts: datetime,
                              broker_exit_side: str,
                              already_used_order_ids: set) -> Optional[dict]:
    """Find a broker order on `broker_exit_side` (sell|buy) that filled
    `qty` shares of `symbol` after `after_ts`.

    Long exit → broker_exit_side='sell' (we sell to close).
    Short cover → broker_exit_side='buy' (we buy to cover).

    Multi-profile sharing means one Alpaca account hosts multiple
    profiles' positions. Each profile's BUY/SHORT has its own
    protective orders, so each profile's exit is a separate broker
    order — match by qty (filled_qty == journal qty). Across profiles
    with the same qty (rare), pick the oldest unused fill so a
    multi-profile pass attributes uniquely.

    Returns dict with order_id, filled_at, filled_qty, filled_avg_price,
    order_type — or None if no match.
    """
    orders, exc = _retrying_call(
        api.list_orders, status="all", symbols=[symbol], limit=200,
    )
    if orders is None:
        return None
    candidates = []
    for o in orders:
        if getattr(o, "side", "") != broker_exit_side:
            continue
        if getattr(o, "status", "") != "filled":
            continue
        oid = getattr(o, "id", None)
        if oid in already_used_order_ids:
            continue
        try:
            filled_qty = float(getattr(o, "filled_qty", 0) or 0)
        except (ValueError, TypeError, AttributeError) as _q_exc:
            # Per-order filled_qty parse loop; skip orders with
            # malformed broker response. Surface for follow-up.
            logger.debug(
                "reconcile filled_qty parse failed: %s: %s",
                type(_q_exc).__name__, _q_exc,
            )
            continue
        if abs(filled_qty - qty) > 0.001:
            continue
        filled_at = getattr(o, "filled_at", None)
        if hasattr(filled_at, "isoformat"):
            fa_dt = filled_at if filled_at.tzinfo else filled_at.replace(tzinfo=timezone.utc)
        else:
            fa_dt = _to_utc_iso(filled_at)
        if fa_dt is None or fa_dt < after_ts:
            continue
        try:
            fill_price = float(getattr(o, "filled_avg_price", 0) or 0)
        except Exception:
            fill_price = 0
        if fill_price <= 0:
            continue
        candidates.append({
            "order_id": oid,
            "filled_at": fa_dt,
            "filled_qty": filled_qty,
            "filled_avg_price": fill_price,
            "order_type": getattr(o, "order_type", "?"),
        })
    if not candidates:
        return None
    candidates.sort(key=lambda c: c["filled_at"])
    return candidates[0]


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
        # so a SELL must have happened.
        sell_fill = _find_matching_exit_fill(
            api, sym, qty, ts or datetime.now(timezone.utc),
            broker_exit_side="sell",
            already_used_order_ids=used_sell_ids,
        )
        if sell_fill is None:
            return "ambiguous", {
                "reason": "entry filled but no matching broker SELL fill found",
            }
        return "backfill", sell_fill
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
        cover_fill = _find_matching_exit_fill(
            api, sym, qty, ts or datetime.now(timezone.utc),
            broker_exit_side="buy",  # buying to cover
            already_used_order_ids=used_cover_ids,
        )
        if cover_fill is None:
            return "ambiguous", {
                "reason": "entry filled but no matching broker BUY (cover) fill found",
            }
        return "backfill", cover_fill
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
         precise. But the columns may be empty (older trades, or paths
         that submitted protective orders without recording the id).
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
        order, _exc = _retrying_call(api.get_order, stop_oid)
        if order is None:
            continue
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
    # FALLBACK: no protective_*_order_id matched. Older trades may
    # not have recorded the protective ID, but the exit may still
    # have fired. Search broker order history for an unused SELL/BUY
    # that matches the journal qty after the BUY's timestamp.
    side = (row["side"] or "").lower()
    expected_exit_side = "buy" if side == "short" else "sell"
    sym = _lookup_symbol_for_row(row)
    ts = _to_utc_iso(row["timestamp"]) or datetime.now(timezone.utc)
    fill = _find_matching_exit_fill(
        api, sym, journal_qty, ts,
        broker_exit_side=expected_exit_side,
        already_used_order_ids=used_sell_ids,
    )
    if fill is not None:
        # Treat as full closure — _find_matching_exit_fill required
        # filled_qty == journal_qty, so this is a complete exit.
        return "backfill_full", fill
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
            "ambiguous": [], "real_held": 0,
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
        rows = _select_open_rows(conn)

        # Seed the dedup set with order_ids already attributed by sibling
        # profiles so the fallback match path doesn't double-attribute one
        # broker fill to multiple profiles.
        used_sell_ids: set = set(cross_profile_used_ids or set())
        used_cover_ids: set = set(cross_profile_used_ids or set())

        for r in rows:
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
            if prot_kind == "backfill_full":
                used_sell_ids.add(prot_detail["order_id"])
                entry_price = float(r["price"] or 0)
                if is_short:
                    actions["backfill_cover"].append({
                        "trade_id": r["id"], "symbol": sym, "qty": qty,
                        "short_price": entry_price,
                        "cover_order_id": prot_detail["order_id"],
                        "cover_price": prot_detail["filled_avg_price"],
                        "cover_qty": prot_detail["filled_qty"],
                        "cover_filled_at": prot_detail["filled_at"].isoformat(),
                        "cover_order_type": prot_detail["order_type"],
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
            elif kind == "backfill":
                if is_short:
                    used_cover_ids.add(detail["order_id"])
                    actions["backfill_cover"].append({
                        "trade_id": r["id"], "symbol": sym, "qty": qty,
                        "short_price": float(r["price"] or 0),
                        "cover_order_id": detail["order_id"],
                        "cover_price": detail["filled_avg_price"],
                        "cover_qty": detail["filled_qty"],
                        "cover_filled_at": detail["filled_at"].isoformat(),
                        "cover_order_type": detail["order_type"],
                    })
                else:
                    used_sell_ids.add(detail["order_id"])
                    actions["backfill_sell"].append({
                        "trade_id": r["id"], "symbol": sym, "qty": qty,
                        "buy_price": float(r["price"] or 0),
                        "sell_order_id": detail["order_id"],
                        "sell_price": detail["filled_avg_price"],
                        "sell_qty": detail["filled_qty"],
                        "sell_filled_at": detail["filled_at"].isoformat(),
                        "sell_order_type": detail["order_type"],
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
                conn.execute(
                    "UPDATE trades SET status='canceled' WHERE id=?",
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
            synthesis_actions = (
                len(actions["backfill_sell"])
                + len(actions["backfill_cover"])
                + len(actions["backfill_partial_sell"])
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
             "ambiguous": 0, "real_held": 0, "errored": 0}

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
        n_r = res["real_held"]

        if not args.quiet or (n_c + n_bs + n_bc + n_bps + n_fp + n_a) > 0:
            print(f"p{p_id:>2} {res['profile'][:30]:<30s}  "
                  f"real={n_r:>3}  cancel={n_c:>2}  bs={n_bs:>2}  "
                  f"bc={n_bc:>2}  bps={n_bps:>2}  fp={n_fp:>2}  amb={n_a:>2}")
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

        grand["cancel"] += n_c
        grand["backfill_sell"] += n_bs
        grand["backfill_cover"] += n_bc
        grand["backfill_partial_sell"] += n_bps
        grand["fix_partial_entry"] += n_fp
        grand["ambiguous"] += n_a
        grand["real_held"] += n_r

    print(f"\n=== TOTALS ===")
    for k, v in grand.items():
        print(f"  {k:<24s}: {v:>3}")
    if not args.apply:
        print(f"\nDry-run only. Re-run with --apply to write changes.")
    if grand["ambiguous"] > 0 or grand["errored"] > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
