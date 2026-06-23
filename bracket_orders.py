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

# Broker order statuses that mean a protective/bracket child is still
# working (will fire). Anything else (canceled/expired/rejected/filled/
# done_for_day) means the protection is GONE and the position needs a
# fresh stop. Shared by has_live_bracket_protection and the sweep's
# naked-bracket re-arm (2026-06-16).
_LIVE_PROTECTIVE_STATUSES = frozenset({
    "new", "accepted", "held", "pending_new", "accepted_for_bidding",
    "pending_replace", "replaced", "partially_filled",
})


def _is_htb_rejection(exc) -> bool:
    """True when a broker order rejection means the asset is hard-to-borrow
    and only accepts DAY orders (a standing GTC protective order is refused).

    Alpaca's message is ``only day orders are allowed for hard-to-borrow
    asset "SYM"``. The asset-level ``easy_to_borrow`` flag does not always
    agree with this — SPCX reports ``easy_to_borrow=True`` yet rejects here —
    so this order-time rejection is the *authoritative* HTB signal."""
    msg = str(exc).lower()
    return (
        "hard-to-borrow" in msg
        or "hard to borrow" in msg
        or "only day orders are allowed" in msg
    )


def _submit_protective(api, kwargs: dict, db_path, symbol: str, describe: str):
    """Submit a protective order GTC, retrying as a DAY order when the
    broker refuses the GTC because the asset is hard-to-borrow.

    Hard-to-borrow names (SPCX et al.) reject every GTC protective stop —
    without this retry the position rides NAKED and we churn the same
    doomed order every cycle. A DAY order IS accepted, so the position is
    protected through the session (the per-cycle polling stop-loss in
    check_exits backstops between cycles and overnight). On the HTB path we
    also LEARN the symbol via ``journal.record_htb_cooldown`` so the entry
    gate stops opening fresh positions in a name we can't protect with a
    standing stop — the asset-flag gate can't catch it because the flag
    itself is wrong (SPCX reports easy_to_borrow=True).

    ``describe`` is the human phrase for logs, e.g.
    ``trailing stop for SPCX (qty=106, trail=5.00%)``. Returns the broker
    order object, or None if it could not be placed.
    """
    try:
        return api.submit_order(time_in_force="gtc", **kwargs)
    except Exception as exc:
        if not _is_htb_rejection(exc):
            logger.warning(
                "Could not place protective %s: %s", describe, exc)
            return None
        # Authoritative HTB signal from the order engine — learn it so we
        # stop opening fresh positions we can't protect, then retry DAY.
        from journal import record_htb_cooldown
        record_htb_cooldown(db_path, symbol)
        try:
            order = api.submit_order(time_in_force="day", **kwargs)
        except Exception as day_exc:
            logger.warning(
                "Could not place protective %s even as a DAY order "
                "(hard-to-borrow): %s", describe, day_exc)
            return None
        logger.warning(
            "Protective %s rejected as GTC (hard-to-borrow); placed as a "
            "DAY order instead and marked the symbol HTB so we stop "
            "opening new positions we can't protect with a standing stop.",
            describe)
        return order


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


def has_live_bracket_protection(api, db_path, symbol) -> bool:
    """True when `symbol`'s most-recent open stock entry in THIS
    profile's journal is protected by live bracket children at the
    broker.

    2026-06-11 — used by check_exits to DEFER polling stop/TP/
    trailing exits for bracket-protected entries. The broker already
    manages the stop+TP atomically; the poll firing in parallel
    submits a full-qty market sell against shares the bracket
    children have reserved → partial fill → fix_partial_sell
    truncates the SELL row while the entry is already flipped
    closed → remainder orphaned at the broker (p97: 4,347 PLUG,
    311 SMCI, 177 NU, 319 IONZ — a −$24.6K equity hole). One owner
    per protection: bracket entries exit via their children or via
    deliberate AI SELLs (which cancel protection first), never via
    the poll."""
    if not db_path:
        return False
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            row = conn.execute(
                "SELECT protective_stop_order_id, "
                "       protective_tp_order_id "
                "FROM trades "
                "WHERE symbol = ? AND side IN ('buy', 'short') "
                "  AND status = 'open' AND occ_symbol IS NULL "
                "ORDER BY id DESC LIMIT 1",
                ((symbol or "").upper(),),
            ).fetchone()
    except sqlite3.Error as exc:
        logger.debug(
            "bracket-protection check failed for %s (%s) — not "
            "deferring", symbol, exc,
        )
        return False
    if not row or not (row[0] or row[1]):
        return False
    # Stamps exist — confirm at least one child is still live at the
    # broker (both terminal would mean protection is gone and the
    # poll is legitimately the last line of defense).
    for oid in (row[0], row[1]):
        if not oid:
            continue
        try:
            order = api.get_order(oid)
            if (getattr(order, "status", "") or "").lower() in (
                _LIVE_PROTECTIVE_STATUSES
            ):
                return True
        except Exception as exc:
            logger.debug(
                "bracket child %s lookup failed for %s: %s",
                str(oid)[:8], symbol, exc,
            )
            continue
    return False


def _heal_bracket_child_tracking(
    conn,
    db_path: Optional[str],
    row,
    parent,
    symbol: str,
    close_side: str,
    abs_qty: int,
) -> None:
    """Backfill missing journal tracking for a bracket entry's child
    legs: protective_*_order_id stamps on the entry row, and
    pending_protective rows for each child the journal doesn't know.

    2026-06-10 (PM) — the at-submit nested fetch races the broker's
    child materialization; when it loses, the entry has NULL stamps
    and the children have no pending rows. The reconciler's
    pending-row contract then reads the eventual child fill as
    orphan synthesis and HALTS the profile (observed on all 13
    profiles in the first post-reset session). This heal runs from
    the protective sweep every cycle, by which time the children
    are visible. Best-effort: failures log debug and retry next
    sweep."""
    try:
        stop_id = stop_trigger = tp_id = tp_trigger = None
        for leg in (getattr(parent, "legs", None) or []):
            ltype = (getattr(leg, "order_type", "") or "").lower()
            lstatus = (getattr(leg, "status", "") or "").lower()
            # Terminal-unfilled legs (the OCO partner of a filled
            # child, expired GTCs) need no forward tracking.
            if lstatus in ("canceled", "expired", "rejected"):
                continue
            if "stop" in ltype:
                stop_id = getattr(leg, "id", None)
                stop_trigger = getattr(leg, "stop_price", None)
            elif "limit" in ltype:
                tp_id = getattr(leg, "id", None)
                tp_trigger = getattr(leg, "limit_price", None)
        if not (stop_id or tp_id):
            return
        # Stamp the entry row where the at-submit stamp is missing.
        try:
            needs_stop = stop_id and not row["protective_stop_order_id"]
            needs_tp = tp_id and not row["protective_tp_order_id"]
        except (KeyError, IndexError):
            needs_stop = needs_tp = False
        if needs_stop or needs_tp:
            conn.execute(
                "UPDATE trades SET "
                "  protective_stop_order_id = COALESCE(?, protective_stop_order_id), "
                "  protective_tp_order_id   = COALESCE(?, protective_tp_order_id) "
                "WHERE id = ?",
                (stop_id if needs_stop else None,
                 tp_id if needs_tp else None,
                 row["id"]),
            )
            conn.commit()
            logger.info(
                "Healed bracket child stamps for %s entry #%s "
                "(stop=%s tp=%s)",
                symbol, row["id"],
                (stop_id or "")[:8], (tp_id or "")[:8],
            )
        # Write pending_protective rows for children the journal
        # doesn't track at all (no row carries their order_id).
        for child_id, signal_type, trigger in (
            (stop_id, "PROTECTIVE_STOP", stop_trigger),
            (tp_id, "PROTECTIVE_TP", tp_trigger),
        ):
            if not child_id:
                continue
            known = conn.execute(
                "SELECT 1 FROM trades WHERE order_id = ? LIMIT 1",
                (child_id,),
            ).fetchone()
            if known:
                continue
            try:
                trigger_f = float(trigger) if trigger else None
            except (TypeError, ValueError):
                trigger_f = None
            _write_pending_protective_row(
                db_path, symbol, close_side, abs_qty, child_id,
                signal_type, trigger_f,
                entry_trade_id=row["id"],
                reason=f"bracket child {signal_type.lower()} "
                       f"(healed by protective sweep); awaiting fill",
            )
    except Exception as _heal_exc:
        logger.debug(
            "Bracket child-tracking heal failed for %s entry #%s "
            "(retries next sweep): %s: %s",
            symbol, row["id"] if row else "?",
            type(_heal_exc).__name__, _heal_exc,
        )


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
    order = _submit_protective(
        api,
        {
            "symbol": symbol,
            "qty": int(qty),
            "side": side,
            "type": "stop",
            "stop_price": round(float(stop_price), 2),
        },
        db_path, symbol,
        f"stop for {symbol} (qty={int(qty)}, stop=${stop_price:.2f})",
    )
    if order is None:
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
    order = _submit_protective(
        api,
        {
            "symbol": symbol,
            "qty": int(qty),
            "side": side,
            "type": "limit",
            "limit_price": round(float(limit_price), 2),
        },
        db_path, symbol,
        f"take-profit for {symbol} (qty={int(qty)}, limit=${limit_price:.2f})",
    )
    if order is None:
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
    order = _submit_protective(
        api,
        {
            "symbol": symbol,
            "qty": int(qty),
            "side": side,
            "type": "trailing_stop",
            "trail_percent": str(round(float(trail_percent), 2)),
        },
        db_path, symbol,
        f"trailing stop for {symbol} (qty={int(qty)}, trail={trail_percent:.2f}%)",
    )
    if order is None:
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

    Why ONE sell-side order per position (not two): Alpaca holds shares
    per open sell-side order. If we submit a trailing stop AND a
    take-profit limit for the same slice, that slice is reserved TWICE —
    so a profile consumes 2× its shares from the shared Alpaca account
    pool, and the NEXT profile's protective stop can't place ('insufficient
    qty available, requested: N, available: M<N'), leaving its position
    NAKED. Verified on prod 2026-06-22: every doubled slice showed
    `pos - sell_reserved == broker available` exactly (no drift, no
    mis-tracking — purely the second reservation). See 2026-06-23 below.

    Order priority:
      1. If `use_trailing_stops`: place trailing_stop ONLY. Functionally
         a superset — it covers downside (initial level = entry × (1 - trail))
         AND locks in gains as the high-water rises.
      2. Else: place static stop ONLY.

    Take-profit is dropped from the broker side; the polling TP check
    in `check_stop_loss_take_profit` still fires at threshold breach.
    TP isn't time-critical the way a stop is.

    2026-06-23 — REVERTED the 2026-06-09 broker-side TP. That change
    placed a second full-qty `limit` order alongside the stop/trailing,
    reintroducing exactly the double-reservation this docstring warns
    about: it caused 51 'insufficient qty available' protective failures
    on 2026-06-22 (NFLX/BMNR/PLUG/ETHA/DFTX…), each leaving a position
    with no fresh broker stop. The broker-side TP also could not work
    reliably — in the default trailing mode it failed ~half the time
    (the trailing already reserved the slice). The downside stop is the
    safety control and must always win the single reservation; the TP
    reverts to per-cycle polling. We also actively CANCEL any lingering
    broker-side TP this entry still tracks (below) so the shares it was
    hogging are freed for the actual stop the same cycle.

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
            # 2026-06-09 — include take_profit column for broker-side
            # TP placement. Test fixtures may use a minimal schema
            # without it; fall back to the legacy column set so
            # placement still runs (just without the TP).
            try:
                row = conn.execute(
                    "SELECT id, protective_stop_order_id, "
                    "protective_tp_order_id, "
                    "protective_trailing_order_id, take_profit "
                    "FROM trades "
                    "WHERE symbol = ? AND side = ? AND status = 'open'"
                    + _occ_filter +
                    " ORDER BY id DESC LIMIT 1",
                    (symbol, entry_side_in_db),
                ).fetchone()
            except sqlite3.OperationalError:
                row = conn.execute(
                    "SELECT id, protective_stop_order_id, "
                    "protective_tp_order_id, "
                    "protective_trailing_order_id "
                    "FROM trades "
                    "WHERE symbol = ? AND side = ? AND status = 'open'"
                    + _occ_filter +
                    " ORDER BY id DESC LIMIT 1",
                    (symbol, entry_side_in_db),
                ).fetchone()
            if not row:
                continue

            # 2026-06-18 — a per-arm "order-id-truth guard" was tried here
            # and REMOVED: the `positions` snapshot already comes from
            # get_virtual_positions (client.get_positions for virtual
            # profiles), which since the same-day fix nets a closed
            # position to 0 — so a flat/closed symbol never enters this
            # sweep and the guard was moot. Worse, its flat signed-sum
            # diverged from get_virtual_positions' FIFO+closed-origin
            # netting and wrongly SKIPPED arming a genuinely-held position
            # when an unbalanced closed row of the opposite sign coexisted,
            # leaving real risk naked. Re-arm prevention now rests on the
            # corrected get_virtual_positions (closed positions aren't in
            # the snapshot) + the per-cycle integrity gate (halts on any
            # broker↔journal drift).

            close_side = "buy" if is_short else "sell"
            abs_qty = abs(int(qty))

            # 2026-06-10 — BRACKET SKIP. If the entry's parent order
            # was submitted as order_class='bracket' (post-rework),
            # the broker is already managing the stop + TP as atomic
            # OCO sub-orders of the entry. This sweep must NOT touch
            # those — the prior behavior cancelled the bracket's TP
            # via _cancel_stale_other_orders within ~5 min of every
            # bracket entry (CCO entry 13:41, TP canceled 13:46).
            # Detect by querying the broker for the entry's parent
            # order_id and checking order_class. Failure-tolerant:
            # if we can't determine, fall through to the legacy
            # behavior so a transient broker blip doesn't leave
            # positions unprotected.
            try:
                _entry_order_id = conn.execute(
                    "SELECT order_id FROM trades WHERE id = ?",
                    (row["id"],),
                ).fetchone()
                if _entry_order_id and _entry_order_id[0]:
                    try:
                        _parent = api.get_order(
                            _entry_order_id[0], nested=True,
                        )
                        if (getattr(_parent, "order_class", "") or "") == "bracket":
                            # The bracket's children handle protection.
                            # Skip; don't risk cancelling live OCO legs.
                            # 2026-06-10 (PM) — HEAL first: the
                            # at-submit nested fetch can race the
                            # broker's child materialization (observed
                            # on every entry of the first post-reset
                            # session), leaving NULL stamps and no
                            # pending rows. This sweep runs every
                            # cycle and the children are long since
                            # visible — stamp the entry row and write
                            # any missing pending_protective rows here
                            # so the reconciler's pending-row contract
                            # holds for bracket children too.
                            _heal_bracket_child_tracking(
                                conn, db_path, row, _parent, symbol,
                                close_side, abs_qty,
                            )
                            # 2026-06-16 — NAKED-BRACKET FIX. Deferring
                            # to "the broker is managing stop+TP" is only
                            # safe if a child is ACTUALLY LIVE. When the
                            # children were canceled (OCO/stale-cancel/
                            # cross-profile) or never materialized, the
                            # position is NAKED and the old code skipped
                            # forever — leaving SUGP −35%, SMR/SNAP with
                            # zero protection. Only defer when a child is
                            # live; otherwise fall through and place a
                            # fresh protective stop. The downstream
                            # broker_coverage/pending dedup prevents a
                            # double-placement if a live child does exist.
                            _live_child = any(
                                (getattr(_leg, "status", "") or "").lower()
                                in _LIVE_PROTECTIVE_STATUSES
                                for _leg in (getattr(_parent, "legs", None)
                                             or [])
                            )
                            if _live_child:
                                logger.debug(
                                    "ensure_protective_stops: skip entry "
                                    "#%s (%s) — parent order_class=bracket "
                                    "with a LIVE child; broker managing "
                                    "stop+TP atomically.",
                                    row["id"], symbol,
                                )
                                continue
                            logger.warning(
                                "ensure_protective_stops: entry #%s (%s) "
                                "was a bracket but has NO live child "
                                "(canceled/never materialized) — position "
                                "is NAKED; placing a fresh protective stop "
                                "instead of deferring to a dead bracket.",
                                row["id"], symbol,
                            )
                            # fall through to placement below
                    except Exception as _bc_exc:
                        logger.debug(
                            "Bracket-class check for entry #%s failed "
                            "(continuing with legacy sweep): %s: %s",
                            row["id"],
                            type(_bc_exc).__name__, _bc_exc,
                        )
            except sqlite3.OperationalError as _legacy_schema_exc:
                # Journal schema without the order_id column (legacy
                # fixtures / pre-migration DBs) — the bracket-class
                # pre-check can't run; fall through to the legacy
                # sweep, which handles those rows correctly. Logged
                # so a schema problem on a REAL journal is visible
                # instead of silently degrading every sweep.
                logger.debug(
                    "Bracket-class pre-check skipped (journal schema "
                    "lacks order_id?): %s — using legacy sweep",
                    _legacy_schema_exc,
                )

            # 2026-06-23 — SUNSET the broker-side take-profit. We no
            # longer place a separate full-qty TP `limit` order (it
            # double-reserved the slice and starved sibling profiles'
            # stops into naked exposure — see the function docstring).
            # Cancel any TP this entry still tracks so its reserved
            # shares are freed for the actual protective stop THIS cycle,
            # then clear the column. Runs before the coverage-skip below
            # so even an already-stop-covered position gets drained.
            # Bracket entries are excluded (they `continue` above) and
            # never carry a sweep-placed protective_tp_order_id.
            _lingering_tp = row["protective_tp_order_id"]
            if _lingering_tp:
                if cancel_protective_stop(api, _lingering_tp):
                    try:
                        conn.execute(
                            "UPDATE trades SET protective_tp_order_id = NULL "
                            "WHERE id = ?",
                            (row["id"],),
                        )
                        conn.commit()
                        logger.info(
                            "Sunset broker-side TP %s for %s (entry #%s) — "
                            "freed its share reservation for the stop.",
                            str(_lingering_tp)[:8], symbol, row["id"],
                        )
                    except sqlite3.Error as _tp_clear_exc:
                        logger.warning(
                            "Cancelled broker-side TP %s for %s but couldn't "
                            "clear the column: %s",
                            str(_lingering_tp)[:8], symbol, _tp_clear_exc,
                        )

            # RC3 (2026-06-05) — JOURNAL-SIDE dedup as the second
            # defense layer behind broker_coverage. Pattern observed:
            # broker_coverage occasionally returns empty for a symbol
            # (Alpaca API blip, cache lag, etc.). When that happens,
            # the original broker_coverage skip-check at line 642
            # falls through and places a duplicate protective stop —
            # producing the BMNR pid=29 pattern of two
            # pending_protective rows for the same entry from
            # different days.
            #
            # The journal records every pending_protective placement
            # at the same timestamp the broker order goes out. If
            # any pending_protective row for this symbol+close_side
            # still has an active broker order behind it, skip the
            # new placement: the existing one is the active coverage,
            # and broker_coverage just couldn't see it this cycle.
            #
            # If the broker can't be reached to verify, default to
            # SKIP. The position keeps its previously-placed
            # protective; next cycle retries. RC1 ensures that when
            # the existing protective eventually fills, the row
            # transitions correctly so the journal stays accurate.
            try:
                _pending_row = conn.execute(
                    "SELECT id, order_id, timestamp FROM trades "
                    "WHERE symbol = ? AND side = ? "
                    "  AND status = 'pending_protective' "
                    "  AND order_id IS NOT NULL AND order_id != '' "
                    "ORDER BY id DESC LIMIT 1",
                    (symbol.upper(), close_side),
                ).fetchone()
            except sqlite3.Error:
                _pending_row = None
            if _pending_row:
                _existing_oid = _pending_row["order_id"]
                try:
                    _existing_order = api.get_order(_existing_oid)
                    _existing_status = (
                        getattr(_existing_order, "status", "") or ""
                    ).lower()
                except Exception as _gex:
                    # Can't verify — safer to skip than place a
                    # potential duplicate. Position keeps its journaled
                    # coverage; next cycle reattempts the check.
                    logger.warning(
                        "ensure_protective_stops: journal records "
                        "pending_protective %s for %s but broker "
                        "check failed (%s); skipping to avoid "
                        "duplicate placement.",
                        _existing_oid[:8], symbol, _gex,
                    )
                    continue
                # Active broker statuses: the protective is in
                # force. Skip placement.
                if _existing_status in (
                    "new", "accepted", "held", "pending_new",
                    "accepted_for_bidding", "pending_replace",
                    "replaced",
                ):
                    continue
                # Terminal-but-unfilled (expired/canceled/rejected):
                # mark the old row terminal so it stops being counted
                # as active coverage, then proceed with new placement.
                if _existing_status in (
                    "expired", "canceled", "rejected", "done_for_day",
                ):
                    try:
                        conn.execute(
                            "UPDATE trades SET status = ? "
                            "WHERE id = ?",
                            (_existing_status, _pending_row["id"]),
                        )
                        conn.commit()
                        logger.info(
                            "ensure_protective_stops: stale "
                            "pending_protective %s (broker status="
                            "%s) marked terminal; placing fresh "
                            "protective for %s",
                            _existing_oid[:8], _existing_status,
                            symbol,
                        )
                    except sqlite3.Error as _ue:
                        logger.warning(
                            "ensure_protective_stops: couldn't mark "
                            "stale pending_protective terminal "
                            "(%s); placing new anyway", _ue,
                        )
                # "filled" status: RC1's _task_update_fills will
                # transition this row to 'closed' next cycle. The
                # position is no longer at the broker (the
                # protective fired), so there's nothing to protect.
                # Skip placement — the entry will close once the
                # state machine catches up.
                if _existing_status == "filled":
                    continue

            # 2026-06-09 (post-reset) — JOURNAL-OWNED coverage only.
            # The pre-2026-06-09 broker-truth check summed ALL broker
            # coverage for (symbol, close_side) across the account
            # and skipped placement when total >= this profile's qty.
            # That's how PAVS today became unprotected on 2 of 3
            # sibling profiles: pid 59 placed a trailing sized to its
            # own 10605 share; pids 56 and 63 then saw the broker's
            # 10605 coverage >= their own ~1k/16k shares and skipped
            # placement. When pid 59's trailing fired at $1.50, pids
            # 56 and 63's positions stayed in at the worse current
            # price (-18.8% from entry).
            #
            # The fix mirrors the sell/cover isolation: a profile
            # owns ONLY the broker orders that its own journal points
            # to. Filter broker_coverage to order_ids tracked in this
            # profile's journal (any open entry's protective_*_order_id)
            # before deciding whether THIS entry is protected. Sibling
            # orders don't count toward our coverage — never did,
            # really; the check just happened to skip without breakage
            # in the single-profile case and silently mis-fired across
            # shared Alpaca accounts.
            own_protective_ids = set()
            try:
                own_rows = conn.execute(
                    "SELECT protective_stop_order_id, "
                    "       protective_tp_order_id, "
                    "       protective_trailing_order_id "
                    "FROM trades WHERE status = 'open' "
                    "  AND (protective_stop_order_id IS NOT NULL "
                    "    OR protective_tp_order_id IS NOT NULL "
                    "    OR protective_trailing_order_id IS NOT NULL)"
                ).fetchall()
                for _r in own_rows:
                    for _oid in _r:
                        if _oid:
                            own_protective_ids.add(_oid)
            except sqlite3.OperationalError:
                # Minimal-schema test fixtures may lack one of the
                # columns; degrade to "no own ids known" so the skip
                # below won't fire spuriously.
                own_protective_ids = set()
            _cover_all = broker_coverage.get(
                (symbol.upper(), close_side), [])
            _cover = [c for c in _cover_all
                      if c.get("order_id") in own_protective_ids]
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
                # Trailing-stop ONLY: it covers downside (initial level =
                # entry × (1 - trail)) AND dynamic profit-lock as the
                # high-water rises. This is the single sell-side
                # reservation for the slice; the AI's fixed profit target
                # is enforced by the per-cycle polling TP. No broker-side
                # TP is placed (it would double-reserve the slice — see
                # the docstring and the TP-sunset above).
                existing_trail_id = row["protective_trailing_order_id"]
                if not (existing_trail_id and _is_order_active(api, existing_trail_id)):
                    # Cancel any stale STATIC stop before re-arming the
                    # trailing so its reservation is freed (the lingering
                    # broker-side TP was already sunset above).
                    _cancel_stale_other_orders(api, conn, row, ("stop",))
                    trail_pct = trail_percent_for_entry(sl_pct)
                    if trail_pct is not None:
                        order_id = submit_protective_trailing(
                            api, symbol, abs_qty, close_side, trail_pct,
                            db_path=db_path, entry_trade_id=row["id"],
                        )
                        if order_id:
                            try:
                                conn.execute(
                                    "UPDATE trades SET protective_trailing_order_id = ? "
                                    "WHERE id = ?",
                                    (order_id, row["id"]),
                                )
                                conn.commit()
                            except Exception as exc:
                                logger.warning(
                                    "Protective trailing placed but couldn't "
                                    "store id: %s (symbol=%s)", exc, symbol,
                                )
            else:
                # Static stop ONLY branch (use_trailing=False). This is
                # the single sell-side reservation for the slice; the AI's
                # profit target is enforced by the per-cycle polling TP.
                existing_stop_id = row["protective_stop_order_id"]
                if not (existing_stop_id and _is_order_active(api, existing_stop_id)):
                    # Cancel any stale trailing before re-arming the static
                    # stop so its reservation is freed (the lingering
                    # broker-side TP was already sunset above).
                    _cancel_stale_other_orders(api, conn, row, ("trailing",))
                    stop_price = stop_price_for_entry(entry_price, sl_pct, is_short)
                    if stop_price is not None:
                        order_id = submit_protective_stop(
                            api, symbol, abs_qty, close_side, stop_price,
                            db_path=db_path, entry_trade_id=row["id"],
                        )
                        if order_id:
                            try:
                                conn.execute(
                                    "UPDATE trades SET protective_stop_order_id = ? "
                                    "WHERE id = ?",
                                    (order_id, row["id"]),
                                )
                                conn.commit()
                            except Exception as exc:
                                logger.warning(
                                    "Protective stop placed but couldn't "
                                    "store id: %s (symbol=%s)", exc, symbol,
                                )

            # NB: no broker-side take-profit is placed here. A separate
            # full-qty TP `limit` would double-reserve the slice and
            # starve sibling profiles' stops into naked exposure (the
            # 2026-06-22 incident — see the docstring). The AI's profit
            # target is enforced every cycle by the polling check in
            # `check_stop_loss_take_profit`; any lingering broker TP from
            # the reverted design is sunset at the top of this loop.

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


def sync_pending_protective_order_ids(api, db_path: str) -> dict:
    """Proactive chain-walk sweep — keep pending_protective rows'
    order_id in sync with the broker's live replace-chain terminal.

    Closes gap #3 from the 2026-06-04 orphan-prevention list: Alpaca
    silently REPLACES trailing-stop orders as the trail bumps
    (server-side, no `submit_order` call). The journal records the
    placement id; if we wait until fill time to walk the chain, we
    can hit max_depth on a long chain. Running this sweep every cycle
    keeps the journal's recorded id within 1-2 hops of the live id,
    so the fill-time chain walk is always near-trivial.

    For each `pending_protective` row in the journal:
      - get_order(row.order_id) to check broker status.
      - If `replaced` / `pending_replace`: walk forward via
        `walk_replace_chain_forward`. UPDATE the row's order_id to
        the live id (and the entry row's protective_*_order_id
        pointer to match).
      - If `canceled` / `expired` / `rejected`: the broker order is
        dead. Mark the row status='canceled' with a reason so it
        stops counting as pending. The next ensure_protective_stops
        sweep will place a new one if the position still needs
        coverage.
      - If `filled`: leave for the reconciler — flip-to-closed
        with full fill data is the reconciler's job, not ours.
      - If alive (`new`/`accepted`/`held`/`pending_new`): no-op.

    Read-only-ish: only writes when state actually drifted at the
    broker. Best-effort; never raises. Caller should run this in
    the same cycle phase as ensure_protective_stops + the existing
    verify_protective_order_sync — they form a three-layer defense:
      1. ensure_protective_stops: ensures EVERY open position has
         broker coverage (per-position truth).
      2. verify_protective_order_sync: invariant check — every
         journaled active id IS live at Alpaca (linkage truth).
      3. sync_pending_protective_order_ids: keeps the journaled id
         CURRENT through Alpaca's replace chain (id-freshness truth).

    Returns: {"checked", "advanced", "marked_canceled", "errored"}.
    """
    if not db_path:
        return {"checked": 0, "advanced": 0,
                "marked_canceled": 0, "errored": 0}
    try:
        from reconcile_journal_to_broker import walk_replace_chain_forward
    except ImportError as exc:
        logger.warning(
            "sync_pending_protective_order_ids: cannot import "
            "walk_replace_chain_forward (%s)", exc,
        )
        return {"checked": 0, "advanced": 0,
                "marked_canceled": 0, "errored": 0}

    stats = {"checked": 0, "advanced": 0,
             "marked_canceled": 0, "errored": 0}
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        logger.warning(
            "sync_pending_protective_order_ids: DB open failed: %s",
            exc,
        )
        return stats

    try:
        try:
            rows = conn.execute(
                "SELECT id, symbol, order_id, signal_type, qty "
                "FROM trades "
                "WHERE status = 'pending_protective' "
                "AND order_id IS NOT NULL AND order_id != ''"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            logger.debug(
                "sync_pending_protective_order_ids: trades table "
                "missing expected columns (%s); skipping", exc,
            )
            return stats

        for row in rows:
            stats["checked"] += 1
            pending_id = row["id"]
            current_oid = row["order_id"]
            symbol = row["symbol"]
            signal_type = (row["signal_type"] or "").upper()

            try:
                order = api.get_order(current_oid)
            except Exception as exc:
                # API blip — leave row untouched, count as errored.
                logger.debug(
                    "sync: get_order failed for %s/%s (%s); skipping",
                    symbol, current_oid[:8], exc,
                )
                stats["errored"] += 1
                continue
            if order is None:
                stats["errored"] += 1
                continue

            status = (getattr(order, "status", "") or "").lower()

            # Alive — nothing to do.
            if status in ("new", "accepted", "held", "pending_new",
                          "accepted_for_bidding"):
                continue
            # Filled — defer to the reconciler.
            if status == "filled":
                continue
            # Replaced — walk the chain forward, update the row.
            if status in _REPLACE_TRANSIENT_STATUSES_BO:
                terminal, depth = walk_replace_chain_forward(
                    api, current_oid)
                if terminal is None:
                    stats["errored"] += 1
                    continue
                terminal_oid = getattr(terminal, "id", None)
                terminal_status = (
                    getattr(terminal, "status", "") or "").lower()
                if not terminal_oid or terminal_oid == current_oid:
                    continue
                # If the terminal is filled, leave for the reconciler
                # (don't pre-empt the closed-with-fill-data write).
                if terminal_status == "filled":
                    continue
                # Otherwise advance the row's order_id to the terminal.
                # Also heal the entry-row pointer column when relevant.
                try:
                    conn.execute(
                        "UPDATE trades SET order_id = ?, "
                        "reason = COALESCE(reason || ' | ', '') || ? "
                        "WHERE id = ?",
                        (terminal_oid,
                         f"sync 2026-06-04: order_id advanced "
                         f"{current_oid[:8]}->{terminal_oid[:8]} "
                         f"after {depth} replace(s)",
                         pending_id),
                    )
                    # Heal entry pointer if it was the old id.
                    col = _entry_pointer_column_for_signal(signal_type)
                    if col:
                        conn.execute(
                            f"UPDATE trades SET {col} = ? "
                            f"WHERE {col} = ?",
                            (terminal_oid, current_oid),
                        )
                    conn.commit()
                    stats["advanced"] += 1
                    logger.info(
                        "sync: advanced %s pending #%d order_id "
                        "%s -> %s (after %d replace(s))",
                        symbol, pending_id, current_oid[:8],
                        terminal_oid[:8], depth,
                    )
                except sqlite3.Error as exc:
                    logger.warning(
                        "sync: failed to advance %s pending #%d: %s",
                        symbol, pending_id, exc,
                    )
                    stats["errored"] += 1
                continue
            # Canceled / expired / rejected — broker order is dead.
            # Mark the row canceled so it stops counting as pending.
            if status in ("canceled", "expired", "rejected"):
                try:
                    conn.execute(
                        "UPDATE trades SET status = 'canceled', pnl = NULL, "
                        "reason = COALESCE(reason || ' | ', '') || ? "
                        "WHERE id = ?",
                        (f"sync 2026-06-04: broker order {current_oid[:8]} "
                         f"status={status} — pending row no longer "
                         f"references a live order",
                         pending_id),
                    )
                    conn.commit()
                    stats["marked_canceled"] += 1
                    logger.info(
                        "sync: marked %s pending #%d canceled "
                        "(broker status=%s)",
                        symbol, pending_id, status,
                    )
                except sqlite3.Error as exc:
                    logger.warning(
                        "sync: failed to cancel %s pending #%d: %s",
                        symbol, pending_id, exc,
                    )
                    stats["errored"] += 1
                continue
            # Any other status — log + continue (don't mutate on
            # statuses we don't recognize).
            logger.debug(
                "sync: %s pending #%d has unrecognized broker "
                "status=%r; leaving untouched",
                symbol, pending_id, status,
            )
    finally:
        conn.close()
    return stats


# Mirror of _REPLACE_TRANSIENT_STATUSES from reconcile_journal_to_broker;
# duplicated here to avoid an import cycle since reconcile uses
# bracket_orders.* helpers in some paths.
_REPLACE_TRANSIENT_STATUSES_BO = frozenset({"replaced", "pending_replace"})


_PROTECTIVE_ENTRY_POINTER_BY_SIGNAL = {
    "PROTECTIVE_STOP": "protective_stop_order_id",
    "PROTECTIVE_TAKE_PROFIT": "protective_tp_order_id",
    "PROTECTIVE_TRAILING": "protective_trailing_order_id",
}


def _entry_pointer_column_for_signal(signal_type: str):
    """Map signal_type -> the entry row's protective_*_order_id column.
    Returns None for unknown signal_types (sync just skips the
    pointer-heal in that case)."""
    return _PROTECTIVE_ENTRY_POINTER_BY_SIGNAL.get(signal_type)


def cancel_for_symbol(api, db_path: str, symbol: str) -> bool:
    """Cancel any active protective stop / take-profit / trailing-stop
    orders for the given symbol.

    Called before a manual exit (AI SELL, polling-triggered exit, etc.)
    so the broker orders don't fire AFTER our market sell on a now-flat
    position.

    Returns True when a protective for this symbol ALREADY FILLED —
    the position is already closed at the broker and the caller MUST
    ABORT its exit. 2026-06-11, the BATL oversell: p97's stop filled
    at 17:52:59; the fill confirmation hadn't reached the journal
    when the next exit fired at 17:55:23, the old void-return cancel
    logged "cancel failed" and the SELL proceeded — selling 5,145
    shares that belonged to a SIBLING profile on the shared account.
    Order status is checked BEFORE attempting each cancel; a filled
    protective keeps its journal pointer (the fill state machine
    needs it) while active ones are canceled and cleared.
    """
    import sqlite3
    if not db_path or not symbol:
        return False
    any_filled = False
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
                row_filled = False
                for oid in (r["protective_stop_order_id"],
                            r["protective_tp_order_id"],
                            r["protective_trailing_order_id"]):
                    if not oid:
                        continue
                    status = ""
                    try:
                        status = (getattr(
                            api.get_order(oid), "status", "") or "").lower()
                    except Exception as _st_exc:
                        logger.debug(
                            "protective %s status lookup failed "
                            "(%s) — attempting cancel anyway",
                            str(oid)[:8], _st_exc,
                        )
                    if status == "filled":
                        any_filled = True
                        row_filled = True
                        logger.warning(
                            "cancel_for_symbol(%s): protective %s "
                            "ALREADY FILLED — position is closed at "
                            "the broker; caller must abort its exit.",
                            symbol, str(oid)[:8],
                        )
                        continue
                    cancel_protective_stop(api, oid)
                if not row_filled:
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
    return any_filled
