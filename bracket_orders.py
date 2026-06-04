"""Broker-managed protective stop orders.

Stage 1 of INTRADAY_STOPS_PLAN.md. Replaces the polling-and-react exit
logic for static stop-losses with broker-side stop orders that fire AT
the stop price when triggered.

Why: polling check_exits runs every 5 minutes. Between cycles, prices
can move significantly past the stop level. The polling logic submits
a market sell at the current price, which is typically far worse than
the intended stop. Real prod data: AMD stop threshold -5%, actual fill
-7.91% — a 60% overshoot.

Submitting a `type='stop'` order with `stop_price = entry × (1 - stop_loss_pct)`
makes the broker fire the moment that price is touched, regardless of
our cycle timing. Fills land at the stop price (or near it on gaps).
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


# 2026-05-21 — Per `feedback_no_orphan_broker_fills`: every api.submit_order
# MUST write a journal row in the same code path. Protective orders
# (stop / take-profit / trailing) were the historical hole — placement
# wrote only the order_id onto the entry row's `protective_*_order_id`
# column, NOT a separate trades row. So when the broker autonomously
# filled the protective order, the reconciler saw a fill with no
# matching trades row and (per safety-net) halted the profile.
#
# Fix: at placement time, write a `status='pending_protective'` row
# carrying the protective order's id + intended trigger. The
# reconciler then just UPDATEs that row on fill — no synthesis path.

_PENDING_PROTECTIVE_STATUS = "pending_protective"


def _write_pending_protective_row(
    db_path: Optional[str],
    symbol: str,
    side: str,
    qty: int,
    order_id: str,
    signal_type: str,
    trigger_price: Optional[float],
    entry_trade_id: Optional[int] = None,
    reason: Optional[str] = None,
) -> bool:
    """Write a placeholder trades row for a protective order at
    PLACEMENT time. Returns True on success, False on failure.

    Per `feedback_no_orphan_broker_fills` (and the 2026-06-04
    atomic-placement upgrade): the CALLER must treat False as a
    placement failure and cancel the broker order to maintain the
    "every broker order has a journal row" invariant. db_path=None
    counts as failure (we can't journal without it).
    """
    if not db_path:
        logger.error(
            "Pending-protective row NOT written for %s/%s order_id=%s — "
            "db_path is None. Caller MUST cancel the broker order to "
            "preserve the no-orphan-broker-fills contract.",
            signal_type, symbol, order_id,
        )
        return False
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                "INSERT INTO trades "
                "(timestamp, symbol, side, qty, price, order_id, "
                " signal_type, status, reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    datetime.utcnow().isoformat(),
                    symbol.upper(),
                    side,
                    int(qty),
                    # NULL for trailing (no fixed trigger); fixed price
                    # for stop / take-profit. trigger_price is the
                    # broker's stop_price / limit_price as submitted.
                    float(trigger_price) if trigger_price else None,
                    order_id,
                    signal_type,
                    _PENDING_PROTECTIVE_STATUS,
                    reason or f"protective {signal_type} placement; "
                              f"awaiting fill",
                ),
            )
            conn.commit()
        return True
    except sqlite3.Error as exc:
        logger.error(
            "Pending-protective journal write FAILED for %s/%s "
            "order_id=%s: %s: %s. Caller MUST cancel the broker order.",
            signal_type, symbol, order_id, type(exc).__name__, exc,
        )
        return False


def _rollback_broker_order(api, order_id: str, why: str) -> None:
    """Cancel a broker order after the journal write failed, so the
    placement is atomic (either both succeed or both are rolled back).

    If cancel itself fails: log CRITICAL — the broker has an order
    with no journal row AND we couldn't undo it. The reconciler's
    fuzzy fallback will detect the eventual fill and halt the profile,
    but at the cost of a stale-data trade. This is the worst case
    the atomic-placement contract is designed to make rare.
    """
    try:
        api.cancel_order(order_id)
        logger.warning(
            "ROLLBACK ok: canceled broker order %s after journal write "
            "failed (%s). No orphan produced.",
            order_id, why,
        )
    except Exception as cancel_exc:
        logger.critical(
            "ROLLBACK FAILED for broker order %s after journal write "
            "failed (%s): cancel raised %s: %s. BROKER HAS AN ORDER "
            "WITHOUT A JOURNAL ROW. The reconciler safety net WILL "
            "halt the profile on the next reconcile pass when the "
            "fill arrives, but the placement itself was not atomic.",
            order_id, why, type(cancel_exc).__name__, cancel_exc,
        )


def submit_protective_stop(
    api,
    symbol: str,
    qty: int,
    side: str,
    stop_price: float,
    db_path: Optional[str] = None,
    entry_trade_id: Optional[int] = None,
) -> Optional[str]:
    """Submit a broker stop order. Returns the order_id on success, None on failure.

    Args:
      api: Alpaca REST client (from client.get_api).
      symbol: Ticker.
      qty: Absolute share count to protect.
      side: "sell" (close a long) or "buy" (cover a short).
      stop_price: Trigger price. Must be below current for sell, above for buy.
      db_path: Per-profile journal DB path. When provided, a
        `pending_protective` trades row is written at placement time
        so the reconciler can UPDATE it on fill (no orphan path).
        Optional only for back-compat with older callers; new
        callers MUST pass it.
      entry_trade_id: Id of the entry trade this protective belongs
        to (for the journal row's reason field). Optional.

    Failure is intentionally non-fatal — if the broker rejects the order
    the polling fallback in check_exits still detects threshold breaches.
    Returns None so the caller knows there's no order_id to track.
    """
    if not symbol or qty <= 0 or stop_price <= 0 or side not in ("sell", "buy"):
        return None
    try:
        order = api.submit_order(
            symbol=symbol,
            qty=int(qty),
            side=side,
            type="stop",
            stop_price=round(float(stop_price), 2),
            time_in_force="gtc",
        )
    except Exception as exc:
        logger.warning(
            "Could not place protective stop for %s (qty=%d, stop=$%.2f): %s",
            symbol, qty, stop_price, exc,
        )
        return None
    order_id = getattr(order, "id", None)
    if not order_id:
        return None
    logger.info(
        "Protective stop placed: %s %s qty=%d stop=$%.2f order_id=%s",
        side, symbol, qty, stop_price, order_id,
    )
    # ATOMIC PLACEMENT per feedback_no_orphan_broker_fills. If the
    # journal write fails, roll back the broker order so we never
    # leave a broker-side order without a journal row.
    journaled = _write_pending_protective_row(
        db_path=db_path,
        symbol=symbol, side=side, qty=qty,
        order_id=order_id,
        signal_type="PROTECTIVE_STOP",
        trigger_price=stop_price,
        entry_trade_id=entry_trade_id,
        reason=(
            f"broker stop @ ${stop_price:.2f}; entry_trade={entry_trade_id}"
            if entry_trade_id else
            f"broker stop @ ${stop_price:.2f}"
        ),
    )
    if not journaled:
        _rollback_broker_order(
            api, order_id, why=f"PROTECTIVE_STOP/{symbol}")
        return None
    return order_id


def cancel_protective_stop(api, order_id: Optional[str]) -> bool:
    """Cancel an open broker stop order. Returns True if cancelled or already gone.

    No-op when order_id is None or empty. Treats already-filled / already-
    cancelled orders as success (the goal is reached either way).
    """
    if not order_id:
        return True
    try:
        api.cancel_order(order_id)
        logger.info("Cancelled protective stop %s", order_id)
        return True
    except Exception as exc:
        # Already-filled / already-cancelled orders raise on cancel — that's
        # not an error from our perspective; the order is no longer live.
        msg = str(exc).lower()
        if "filled" in msg or "cancel" in msg or "not found" in msg or "404" in msg:
            return True
        logger.warning("Could not cancel protective stop %s: %s", order_id, exc)
        return False


def stop_price_for_entry(
    entry_price: float,
    stop_loss_pct: float,
    is_short: bool,
) -> Optional[float]:
    """Compute the appropriate stop_price given entry and risk parameters.

    Long: entry × (1 - stop_loss_pct). Stop fires when price falls.
    Short: entry × (1 + stop_loss_pct). Stop fires when price rises.

    Returns None on invalid inputs (zero entry, missing pct).
    """
    if not entry_price or entry_price <= 0:
        return None
    if stop_loss_pct is None or stop_loss_pct <= 0:
        return None
    if is_short:
        return entry_price * (1 + stop_loss_pct)
    return entry_price * (1 - stop_loss_pct)


def tp_price_for_entry(
    entry_price: float,
    take_profit_pct: float,
    is_short: bool,
) -> Optional[float]:
    """Compute the take-profit limit price.

    Long: entry × (1 + take_profit_pct). Limit fires when price hits target.
    Short: entry × (1 - take_profit_pct).
    """
    if not entry_price or entry_price <= 0:
        return None
    if take_profit_pct is None or take_profit_pct <= 0:
        return None
    if is_short:
        return entry_price * (1 - take_profit_pct)
    return entry_price * (1 + take_profit_pct)


def submit_protective_take_profit(
    api,
    symbol: str,
    qty: int,
    side: str,
    limit_price: float,
    db_path: Optional[str] = None,
    entry_trade_id: Optional[int] = None,
) -> Optional[str]:
    """Submit a broker limit order to lock in profit at a target level.

    Use type='limit' (not stop) — fills only when price meets or beats
    the target. Won't slip past the limit on gaps; will simply not fill
    if the target is never reached. Pairs with the protective stop on
    the downside.

    `db_path` and `entry_trade_id` enable atomic journaling: a
    `pending_protective` trades row is written on placement so the
    reconciler can UPDATE it on fill without a synthesis path.
    """
    if not symbol or qty <= 0 or limit_price <= 0 or side not in ("sell", "buy"):
        return None
    try:
        order = api.submit_order(
            symbol=symbol,
            qty=int(qty),
            side=side,
            type="limit",
            limit_price=round(float(limit_price), 2),
            time_in_force="gtc",
        )
    except Exception as exc:
        logger.warning(
            "Could not place protective take-profit for %s (qty=%d, limit=$%.2f): %s",
            symbol, qty, limit_price, exc,
        )
        return None
    order_id = getattr(order, "id", None)
    if not order_id:
        return None
    logger.info(
        "Protective take-profit placed: %s %s qty=%d limit=$%.2f order_id=%s",
        side, symbol, qty, limit_price, order_id,
    )
    # ATOMIC PLACEMENT — roll back broker order on journal failure.
    journaled = _write_pending_protective_row(
        db_path=db_path,
        symbol=symbol, side=side, qty=qty,
        order_id=order_id,
        signal_type="PROTECTIVE_TAKE_PROFIT",
        trigger_price=limit_price,
        entry_trade_id=entry_trade_id,
        reason=(
            f"broker take-profit @ ${limit_price:.2f}; "
            f"entry_trade={entry_trade_id}"
            if entry_trade_id else
            f"broker take-profit @ ${limit_price:.2f}"
        ),
    )
    if not journaled:
        _rollback_broker_order(
            api, order_id, why=f"PROTECTIVE_TAKE_PROFIT/{symbol}")
        return None
    return order_id


# Bounds on trail percent to avoid stops that are too tight (whipsaw
# on normal volatility) or too loose (defeats the purpose).
TRAIL_PERCENT_MIN = 2.0
TRAIL_PERCENT_MAX = 10.0


def trail_percent_for_entry(stop_loss_pct: float) -> Optional[float]:
    """Convert the profile's stop_loss_pct to an Alpaca trail_percent.

    Uses the same percent the user accepts for the static stop. If
    stop_loss_pct=0.05, the trail follows the high water at 5% below.
    Clamped to [2%, 10%] so we don't get tight-stop whipsaws on
    high-vol names or worthless 20% trails on low-vol names.

    Returns None on invalid inputs.
    """
    if stop_loss_pct is None or stop_loss_pct <= 0:
        return None
    pct = stop_loss_pct * 100.0
    return max(TRAIL_PERCENT_MIN, min(TRAIL_PERCENT_MAX, pct))


def submit_protective_trailing(
    api,
    symbol: str,
    qty: int,
    side: str,
    trail_percent: float,
    db_path: Optional[str] = None,
    entry_trade_id: Optional[int] = None,
) -> Optional[str]:
    """Submit a broker trailing-stop order.

    Alpaca tracks the high water continuously and adjusts the stop level
    to (high - trail_percent% × high). When price falls through the
    level, fires a market order. This eliminates the polling lag that
    caused IBM-style "intraday spike then EOD collapse" giveback.

    side='sell' for long position close, 'buy' for short cover.

    `db_path` and `entry_trade_id` enable atomic journaling: a
    `pending_protective` trades row is written on placement so the
    reconciler can UPDATE it on fill without a synthesis path. The
    journal row's `price` is NULL (trailing stops have no fixed
    trigger; the broker continuously adjusts the stop level).
    """
    if not symbol or qty <= 0 or trail_percent <= 0 or side not in ("sell", "buy"):
        return None
    try:
        order = api.submit_order(
            symbol=symbol,
            qty=int(qty),
            side=side,
            type="trailing_stop",
            trail_percent=str(round(float(trail_percent), 2)),
            time_in_force="gtc",
        )
    except Exception as exc:
        logger.warning(
            "Could not place protective trailing stop for %s "
            "(qty=%d, trail=%.2f%%): %s",
            symbol, qty, trail_percent, exc,
        )
        return None
    order_id = getattr(order, "id", None)
    if not order_id:
        return None
    logger.info(
        "Protective trailing stop placed: %s %s qty=%d trail=%.2f%% order_id=%s",
        side, symbol, qty, trail_percent, order_id,
    )
    # ATOMIC PLACEMENT — roll back broker order on journal failure.
    journaled = _write_pending_protective_row(
        db_path=db_path,
        symbol=symbol, side=side, qty=qty,
        order_id=order_id,
        signal_type="PROTECTIVE_TRAILING",
        trigger_price=None,  # trailing has no fixed price
        entry_trade_id=entry_trade_id,
        reason=(
            f"broker trailing-stop {trail_percent:.2f}%; "
            f"entry_trade={entry_trade_id}"
            if entry_trade_id else
            f"broker trailing-stop {trail_percent:.2f}%"
        ),
    )
    if not journaled:
        _rollback_broker_order(
            api, order_id, why=f"PROTECTIVE_TRAILING/{symbol}")
        return None
    return order_id


def _is_order_active(api, order_id: str) -> bool:
    """Return True iff the order is still working at the broker. Fail-open
    on lookup errors — we'd rather submit a duplicate than leave a position
    unprotected because the API blipped."""
    if not order_id:
        return False
    try:
        order = api.get_order(order_id)
    except Exception:
        return False
    status = (getattr(order, "status", "") or "").lower()
    return status in ("new", "accepted", "pending_new", "held",
                       "accepted_for_bidding")


_ACTIVE_ORDER_STATUSES = frozenset({
    "new", "accepted", "pending_new", "held", "accepted_for_bidding",
    "partially_filled",
})
_PROTECTIVE_ORDER_TYPES = frozenset({"stop", "trailing_stop", "stop_limit"})


def active_protective_coverage(api):
    """Return broker-truth protective coverage, keyed by (symbol, side).

    2026-05-21 — the canonical source of "is this position protected?"
    is the BROKER, not a re-derived journal lookup. This pulls every
    currently-working protective order (stop / trailing_stop /
    stop_limit) from Alpaca ONCE and buckets them by (symbol,
    close-side) so `ensure_protective_stops` can decide skip-vs-place
    against reality instead of guessing which journal row owns the
    position.

    Returns: dict {(symbol, side): [ {order_id, qty, type}, ... ]}
    where side is 'sell' (protects a long) or 'buy' (protects a short).
    Empty dict on API error (caller falls back to per-position
    placement — fail-open, never leave a position unprotected because
    the bulk fetch blipped).
    """
    out = {}
    try:
        orders = api.list_orders(status="open", limit=500)
    except Exception as exc:
        logger.warning(
            "active_protective_coverage: list_orders failed (%s: %s) — "
            "falling back to per-position placement",
            type(exc).__name__, exc,
        )
        return out
    for o in orders or []:
        otype = (getattr(o, "order_type", None)
                 or getattr(o, "type", "") or "").lower()
        if otype not in _PROTECTIVE_ORDER_TYPES:
            continue
        status = (getattr(o, "status", "") or "").lower()
        if status not in _ACTIVE_ORDER_STATUSES:
            continue
        sym = (getattr(o, "symbol", "") or "").upper()
        side = (getattr(o, "side", "") or "").lower()
        if not sym or side not in ("sell", "buy"):
            continue
        try:
            qty = abs(float(getattr(o, "qty", 0) or 0))
        except (TypeError, ValueError):
            qty = 0.0
        out.setdefault((sym, side), []).append({
            "order_id": getattr(o, "id", None),
            "qty": qty,
            "type": otype,
        })
    return out


def ensure_protective_stops(api, positions, ctx, db_path,
                              conviction_tp_skip=None):
    """Sweep all open positions and place ONE broker protective order
    per position.

    Called from trader.check_exits each cycle. Idempotent — verifies
    the stored protective order id is still working before deciding
    to submit a new one. Survives restarts and races with the entry
    path.

    Why one order per position (not three): Alpaca treats every open
    sell-side order as a qty reservation against the position. If we
    submit a stop, take-profit AND trailing on a 19-share SBUX
    position, the first one reserves all 19 shares — the next two
    fail with 'insufficient qty available, requested: 19, available: 0'.
    Verified pattern on prod 2026-04-30.

    Order priority:
      1. If `use_trailing_stops`: place trailing_stop ONLY. Functionally
         a superset — it covers downside (initial level = entry × (1 - trail))
         AND locks in gains as the high-water rises.
      2. Else: place static stop ONLY.

    Take-profit is dropped from the broker side; the polling TP check
    in `check_stop_loss_take_profit` still fires at threshold breach.
    TP isn't time-critical the way stops are.

    conviction_tp_skip: when this returns True for a position (the
    runaway-winner override), skip the trailing stop entirely (let the
    winner run; the polling stop-loss is the only guard).
    """
    import sqlite3
    if not db_path or not positions:
        return
    sl_pct_long = getattr(ctx, "stop_loss_pct", None) if ctx else None
    sl_pct_short = (getattr(ctx, "short_stop_loss_pct", None) or sl_pct_long
                     if ctx else None)
    if not sl_pct_long and not sl_pct_short:
        return

    use_trailing = (getattr(ctx, "use_trailing_stops", False)
                     if ctx else False)

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
    except Exception:
        return

    # Probe for the occ_symbol column once. Real prod trades tables
    # always have it (migration); minimal test fixtures may not. The
    # entry-row lookup below filters option legs out only when the
    # column exists.
    try:
        _has_occ = bool(conn.execute(
            "SELECT COUNT(*) FROM pragma_table_info('trades') "
            "WHERE name = 'occ_symbol'"
        ).fetchone()[0])
    except Exception:
        _has_occ = False
    _occ_filter = (
        " AND (occ_symbol IS NULL OR occ_symbol = '')" if _has_occ else ""
    )

    # 2026-05-21 — pull broker-truth protective coverage ONCE per
    # sweep. The skip-vs-place decision below is made against this
    # (keyed on Alpaca order_id) instead of a fuzzy journal lookup.
    # Eliminates the FCX-class bug where a symbol held as BOTH stock
    # and option legs caused the journal lookup to grab the wrong row
    # and re-attempt an already-placed protective order every cycle.
    broker_coverage = active_protective_coverage(api)

    try:
        for pos in positions:
            # Skip option positions — defined-risk multileg spreads
            # are bounded by entry debit (no per-leg stock-style
            # protection makes sense), and single-leg long options
            # need OCC-side exits via the option lifecycle path
            # (TODO). Stock-side trailing stops on the underlying
            # would mis-route to the wrong instrument and fire as
            # unintended SHORT orders if triggered (the 2026-05-11
            # phantom-stops incident — 23 armed across 2 accounts).
            # Phase 2 of Position class refactor: uses pos.is_option
            # directly. Phase 1's _is_occ_symbol heuristic guard is
            # replaced by canonical attribute access.
            if getattr(pos, "is_option", False) or pos.get("occ_symbol"):
                continue

            symbol = pos.get("symbol")
            qty = float(pos.get("qty", 0))
            entry_price = float(pos.get("avg_entry_price", 0))
            if not symbol or qty == 0 or entry_price <= 0:
                continue

            is_short = qty < 0
            entry_side_in_db = "short" if is_short else "buy"
            # 2026-05-21 — EXCLUDE option legs from the entry-row
            # lookup. This loop only protects STOCK positions (option
            # rows skipped at the top via pos.is_option). But the
            # journal lookup matched by symbol+side ONLY, so for a
            # symbol held BOTH as stock AND as option legs (e.g. pid24
            # FCX: a 418-share stock BUY + FCX bear_call_spread legs),
            # `ORDER BY id DESC LIMIT 1` grabbed the most-recent
            # OPTION leg row — which has no protective_trailing_order_id.
            # The skip-check then saw "no existing protection" and
            # tried to place a NEW trailing stop on the stock, failing
            # every cycle with "insufficient qty available" because
            # the stock's REAL protective order (recorded on the stock
            # entry row) already reserved all the shares. Restricting
            # to occ_symbol IS NULL picks the actual stock entry row,
            # so its protective_trailing_order_id is found and the
            # already-protected position is correctly skipped.
            row = conn.execute(
                "SELECT id, protective_stop_order_id, protective_tp_order_id, "
                "protective_trailing_order_id "
                "FROM trades "
                "WHERE symbol = ? AND side = ? AND status = 'open'"
                + _occ_filter +
                " ORDER BY id DESC LIMIT 1",
                (symbol, entry_side_in_db),
            ).fetchone()
            if not row:
                continue

            close_side = "buy" if is_short else "sell"
            abs_qty = abs(int(qty))

            # 2026-05-21 — BROKER-TRUTH skip decision. If Alpaca
            # already has active protective coverage for this
            # (symbol, close_side) that meets/exceeds the position
            # qty, the position IS protected — skip placement. Heal
            # the journal entry-row pointer to the live order_id when
            # it's missing or stale so journal == Alpaca (keyed on
            # order_id). This replaces the prior logic that re-derived
            # protection status from the entry row's stored id alone
            # — which broke when the wrong row was matched or the
            # pointer drifted, causing endless "insufficient qty
            # available" retries on already-protected positions.
            _cover = broker_coverage.get((symbol.upper(), close_side), [])
            _covered_qty = sum(c["qty"] for c in _cover)
            if _cover and _covered_qty >= abs_qty - 0.001:
                # Already protected at the broker. Heal the journal
                # pointer if it doesn't already name a live order.
                _live_ids = {c["order_id"] for c in _cover if c["order_id"]}
                _recorded = (
                    row["protective_trailing_order_id"]
                    or row["protective_stop_order_id"]
                )
                if _recorded not in _live_ids:
                    # Prefer the trailing order's column when the live
                    # coverage is a trailing stop; else the stop column.
                    _has_trailing = any(
                        c["type"] == "trailing_stop" for c in _cover)
                    _heal_col = (
                        "protective_trailing_order_id" if _has_trailing
                        else "protective_stop_order_id"
                    )
                    _heal_id = sorted(_live_ids)[0] if _live_ids else None
                    if _heal_id:
                        try:
                            conn.execute(
                                f"UPDATE trades SET {_heal_col} = ? "
                                f"WHERE id = ?",
                                (_heal_id, row["id"]),
                            )
                            conn.commit()
                            logger.info(
                                "Healed protective linkage for %s: "
                                "entry trade #%s -> live broker order %s "
                                "(was %r)",
                                symbol, row["id"], _heal_id[:8],
                                _recorded,
                            )
                        except Exception as _heal_exc:
                            logger.warning(
                                "Could not heal protective linkage for "
                                "%s: %s", symbol, _heal_exc,
                            )
                continue

            # Conviction-override: runaway winner — let it run, no
            # trail cap. Polling stop-loss is the only guard.
            if conviction_tp_skip is not None:
                try:
                    cur_price = float(pos.get("current_price") or 0)
                    pct_change = ((cur_price - entry_price) / entry_price
                                   if entry_price > 0 and cur_price > 0 else 0)
                    if conviction_tp_skip(symbol, pct_change):
                        continue
                except (ImportError, AttributeError, KeyError, ValueError,
                        TypeError) as _ct_exc:
                    # Conviction-TP skip eval; falls through to
                    # standard SL/TP placement. Surface for follow-up.
                    logger.debug(
                        "conviction-TP skip eval failed: %s: %s",
                        type(_ct_exc).__name__, _ct_exc,
                    )

            sl_pct = sl_pct_short if is_short else sl_pct_long

            if use_trailing:
                # Trailing-stop: covers BOTH downside AND profit-lock.
                # Skip if already in place.
                existing_trail_id = row["protective_trailing_order_id"]
                if existing_trail_id and _is_order_active(api, existing_trail_id):
                    continue
                # Free up qty by cancelling any stale stop/TP this row
                # may have from a previous deploy that placed all three.
                # Without this, the old reservations block the new trail.
                _cancel_stale_other_orders(api, conn, row, ("stop", "tp"))
                trail_pct = trail_percent_for_entry(sl_pct)
                if trail_pct is None:
                    continue
                order_id = submit_protective_trailing(
                    api, symbol, abs_qty, close_side, trail_pct,
                    db_path=db_path, entry_trade_id=row["id"],
                )
                column = "protective_trailing_order_id"
            else:
                # Static stop only. No TP — that goes through polling.
                existing_stop_id = row["protective_stop_order_id"]
                if existing_stop_id and _is_order_active(api, existing_stop_id):
                    continue
                _cancel_stale_other_orders(api, conn, row, ("tp", "trailing"))
                stop_price = stop_price_for_entry(entry_price, sl_pct, is_short)
                if stop_price is None:
                    continue
                order_id = submit_protective_stop(
                    api, symbol, abs_qty, close_side, stop_price,
                    db_path=db_path, entry_trade_id=row["id"],
                )
                column = "protective_stop_order_id"

            if order_id:
                try:
                    conn.execute(
                        f"UPDATE trades SET {column} = ? WHERE id = ?",
                        (order_id, row["id"]),
                    )
                    conn.commit()
                except Exception as exc:
                    logger.warning(
                        "Protective order placed but couldn't store id: %s "
                        "(symbol=%s, column=%s)", exc, symbol, column,
                    )
    finally:
        try:
            conn.close()
        except sqlite3.ProgrammingError as _cl_exc:
            # Finally-block conn cleanup; conn may already be
            # closed. Surface for follow-up.
            logger.debug(
                "bracket_orders conn close: %s: %s",
                type(_cl_exc).__name__, _cl_exc,
            )


def _cancel_stale_other_orders(api, conn, row, kinds):
    """Cancel protective orders of the given kinds (stop/tp/trailing)
    on this trade row and clear the columns. Used during the
    one-order-per-position migration so legacy stop+TP+trailing
    triplets get reduced to whichever single order the new sweep
    decided to place."""
    column_map = {
        "stop": "protective_stop_order_id",
        "tp": "protective_tp_order_id",
        "trailing": "protective_trailing_order_id",
    }
    cleared_cols = []
    for kind in kinds:
        col = column_map.get(kind)
        if not col:
            continue
        order_id = row[col] if col in row.keys() else None
        if order_id:
            cancel_protective_stop(api, order_id)
            cleared_cols.append(col)
    if cleared_cols:
        try:
            sets = ", ".join(f"{c} = NULL" for c in cleared_cols)
            conn.execute(f"UPDATE trades SET {sets} WHERE id = ?",
                         (row["id"],))
            conn.commit()
        except Exception as exc:
            logger.debug("Couldn't clear stale order columns: %s", exc)


def has_active_broker_trailing(api, db_path: str, symbol: str) -> bool:
    """Return True iff this profile has a tracked broker trailing-stop
    order for `symbol` that's still working at Alpaca.

    Used by trader.check_exits to defer the polling-based trailing
    detection — if the broker is going to fire at the trail level on
    a tick basis, polling on a 5-minute snapshot would only beat it
    to a worse price. Polling stays as the fallback when there's no
    active broker trailing.
    """
    import sqlite3
    if not db_path or not symbol:
        return False
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            row = conn.execute(
                "SELECT protective_trailing_order_id FROM trades "
                "WHERE symbol = ? AND side = 'buy' AND status = 'open' "
                "AND protective_trailing_order_id IS NOT NULL "
                "ORDER BY id DESC LIMIT 1",
                (symbol,),
            ).fetchone()
        if not row or not row[0]:
            return False
        return _is_order_active(api, row[0])
    except Exception:
        return False


def verify_protective_order_sync(api, db_path: str) -> dict:
    """Order_id-keyed invariant: every protective order_id the
    journal records as ACTIVE must be live at Alpaca.

    This is the "journal == Alpaca, always" guarantee — keyed on the
    canonical Alpaca order_id, not re-derived by symbol heuristics.
    It catches the stale-linkage class deterministically:

      - The journal points an open entry row's protective_*_order_id
        (or a pending_protective row's order_id) at an order that is
        NOT live at the broker → STALE. Either the order fired/
        canceled and the reconciler hasn't updated the row, or the
        pointer drifted. This is exactly the FCX-class drift that
        spammed "insufficient qty available" every cycle.

    Direction note: only the journal→Alpaca direction is checked
    per-profile. The Alpaca→journal direction (a live broker order
    with no journal row) is account-level — Alpaca's list_orders is
    shared across the profiles on one account, so it can't be
    attributed to a single profile's journal here. The atomic-
    journaling contract (every submit_order writes a row) plus the
    reconciler's orphan-fill safety net cover that direction.

    Pure read — never mutates. Returns:
      {"stale": [ {order_id, symbol, source} ... ],
       "verified": int}   # count of journal ids confirmed live
    """
    import sqlite3
    if not db_path:
        return {"stale": [], "verified": 0}
    recorded = []  # (order_id, symbol, source)
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            # Active protective pointers on open entry rows
            for col in ("protective_stop_order_id",
                        "protective_tp_order_id",
                        "protective_trailing_order_id"):
                try:
                    rows = conn.execute(
                        f"SELECT {col} AS oid, symbol FROM trades "
                        f"WHERE status = 'open' AND {col} IS NOT NULL "
                        f"AND {col} != ''",
                    ).fetchall()
                except sqlite3.OperationalError:
                    continue  # column absent on a minimal schema
                for r in rows:
                    recorded.append((r["oid"], r["symbol"], col))
            # pending_protective rows (written at placement; reconciler
            # flips to 'closed' on fill)
            try:
                rows = conn.execute(
                    "SELECT order_id AS oid, symbol FROM trades "
                    "WHERE status = 'pending_protective' "
                    "AND order_id IS NOT NULL AND order_id != ''",
                ).fetchall()
                for r in rows:
                    recorded.append((r["oid"], r["symbol"],
                                     "pending_protective"))
            except sqlite3.OperationalError as _pp_exc:
                # status/order_id column absent on a minimal schema —
                # skip the pending_protective scan; the entry-pointer
                # scan above still runs. Logged (not silent) per the
                # broker-submit-invariant: no bare except-pass on a
                # DB call.
                logger.debug(
                    "verify_protective_order_sync: pending_protective "
                    "scan skipped (%s)", _pp_exc,
                )
    except Exception as exc:
        logger.warning(
            "verify_protective_order_sync: journal read failed "
            "(%s: %s)", type(exc).__name__, exc,
        )
        return {"stale": [], "verified": 0}

    stale = []
    verified = 0
    # Dedup by order_id (the same id can appear on both the entry
    # pointer and a pending_protective row).
    seen = {}
    for oid, sym, source in recorded:
        if oid in seen:
            continue
        seen[oid] = True
        if _is_order_active(api, oid):
            verified += 1
        else:
            stale.append({"order_id": oid, "symbol": sym,
                          "source": source})
    return {"stale": stale, "verified": verified}


def cancel_for_symbol(api, db_path: str, symbol: str) -> None:
    """Cancel any active protective stop / take-profit / trailing-stop
    orders for the given symbol.

    Called before a manual exit (AI SELL, polling-triggered exit, etc.)
    so the broker orders don't fire AFTER our market sell on a now-flat
    position. The matching trade row's protective_*_order_id columns
    are cleared either way (cancel succeeded, or the order is already gone).
    """
    import sqlite3
    if not db_path or not symbol:
        return
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, protective_stop_order_id, protective_tp_order_id, "
                "protective_trailing_order_id "
                "FROM trades "
                "WHERE symbol = ? AND status = 'open' "
                "AND (protective_stop_order_id IS NOT NULL "
                "     OR protective_tp_order_id IS NOT NULL "
                "     OR protective_trailing_order_id IS NOT NULL)",
                (symbol,),
            ).fetchall()
            for r in rows:
                if r["protective_stop_order_id"]:
                    cancel_protective_stop(api, r["protective_stop_order_id"])
                if r["protective_tp_order_id"]:
                    cancel_protective_stop(api, r["protective_tp_order_id"])
                if r["protective_trailing_order_id"]:
                    cancel_protective_stop(api, r["protective_trailing_order_id"])
                conn.execute(
                    "UPDATE trades SET protective_stop_order_id = NULL, "
                    "protective_tp_order_id = NULL, "
                    "protective_trailing_order_id = NULL "
                    "WHERE id = ?",
                    (r["id"],),
                )
            conn.commit()
    except Exception as exc:
        logger.debug("cancel_for_symbol(%s) skipped: %s", symbol, exc)
