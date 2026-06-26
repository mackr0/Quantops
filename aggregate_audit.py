"""Cross-profile aggregate audit: journal totals vs broker truth.

Every Alpaca account hosts multiple profiles (memory: virtual-account
architecture). The per-profile reconcile in
`reconcile_journal_to_broker` keeps each profile's journal aligned
with what *that profile* should hold. But because the broker has only
a single account-level view of each symbol, an additional aggregate
check is needed:

    For each shared Alpaca account A and each symbol S:
       sum(virtual_position(profile, S) for profile routing to A)
                              must equal
                       broker.list_positions(S) on A

When they differ:
  - Broker > journal sum: orphan shares — broker holds them but no
    profile owns them. Possible causes: manual broker action, a code
    path that submitted an order without journal logging, a stale
    journal close that should have stayed open.
  - Journal sum > broker: phantom claim — profiles think they own
    shares that aren't at the broker. The per-profile reconcile
    should already detect this; if it shows up here, the per-profile
    reconcile didn't catch it (e.g. partial drift across multiple
    profiles' journals).

This audit was added 2026-05-06 as defense-in-depth alongside the
pre-trade overshoot guard (`order_guard.allowable_sell_qty`). The
guard prevents the cumulative-overshoot bug at submission time. The
audit catches anything that bypasses the guard — manual orders,
future code that forgets the guard, race conditions.

Usage:
    from models import get_active_profile_ids
    audit = audit_aggregate_drift(profile_ids=get_active_profile_ids())
    if audit['drift']:
        # Alert / email / log loud
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# Tolerance for fractional-share noise. Round to 2 decimals so we
# don't false-alarm on 0.0001-share residuals from prior partial fills.
_QTY_TOLERANCE = 0.05


def _journal_open_qty_per_symbol(db_path: str) -> Dict[str, float]:
    """Sum of open virtual qty per symbol for one profile's journal.

    Aggregates by `occ_symbol` if set (option contracts use OCC at the
    broker), else by underlying. This matches Alpaca's
    list_positions output exactly so the audit doesn't false-flag
    multi-leg option trades whose journal rows store
    symbol="MSFT" + occ_symbol="MSFT260612P00375000".

    Long positions (side=buy) add positive qty; shorts (side=short)
    subtract; matching SELL/COVER consume their respective lots
    via FIFO so closed round-trips net to zero.
    """
    import sqlite3 as _sqlite3
    out: Dict[str, float] = {}
    # Long lots and short lots tracked per (effective) symbol so a
    # profile can have both a long and a short on the same name without
    # the FIFO consuming the wrong side.
    long_lots: Dict[str, list] = {}
    short_lots: Dict[str, list] = {}
    try:
        conn = _sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cur = conn.execute("PRAGMA table_info(trades)")
        cols = {r[1] for r in cur.fetchall()}
        select_cols = "side, qty, price"
        has_occ = "occ_symbol" in cols
        if has_occ:
            select_cols = "COALESCE(occ_symbol, symbol) as eff_symbol, " + select_cols
        else:
            select_cols = "symbol as eff_symbol, " + select_cols
        # Live-state filter (2026-05-16 + 2026-05-17 refinement):
        # only LIVE rows (status='open' / 'pending_fill') contribute
        # to current journal position. Closed rows are HISTORY; they
        # already netted to zero in their lifetime and re-including
        # them in FIFO would cause stale SELLs to consume unrelated
        # newer BUYs (caught when the AUTO_RECONCILE backfill of
        # STRC was being eaten by a closed-but-still-included SELL
        # from an earlier round-trip).
        _LIVE_STATUSES = ("open", "pending_fill")
        rows = conn.execute(
            f"SELECT {select_cols} FROM trades "
            "WHERE COALESCE(status, 'open') IN ('open', 'pending_fill') "
            "ORDER BY timestamp ASC, id ASC"
        ).fetchall()
        conn.close()
    except Exception:
        return out

    for row in rows:
        sym = (row[0] or "").upper()
        side = (row[1] or "").lower()
        qty = float(row[2] or 0)
        price = float(row[3] or 0)
        if qty <= 0:
            continue
        # NOTE: price=0 is allowed for the audit (option log_trade
        # calls store NULL price; the audit only needs qty for
        # FIFO matching). Don't filter on price.
        if side == "buy":
            long_lots.setdefault(sym, []).append([qty, price])
        elif side == "short":
            short_lots.setdefault(sym, []).append([qty, price])
        elif side == "sell":
            ll = long_lots.setdefault(sym, [])
            remaining = qty
            while remaining > 0 and ll:
                consumed = min(ll[0][0], remaining)
                ll[0][0] -= consumed
                remaining -= consumed
                if ll[0][0] <= 0.001:
                    ll.pop(0)
            # 2026-06-22 — book the sell REMAINDER as a short, mirroring
            # get_virtual_positions (journal.py). A sell beyond any long is
            # a real broker short: an option sell-to-open leg (the short
            # side of a bear-call / bull-put / etc. spread) or a stock
            # oversell. The old code DISCARDED this remainder, so every
            # live option short leg read journal_qty=0 → a FALSE
            # broker_orphan on /issues, and a real stock oversell short
            # would have been hidden from the aggregate drift audit. The
            # live-status filter above already excludes closed /
            # auto_reconciled_phantom_close rows, so only genuinely-open
            # shorts are booked (matching get_virtual's occ exclusion).
            if remaining > 0.001:
                short_lots.setdefault(sym, []).append([remaining, price])
        elif side == "cover":
            sl = short_lots.setdefault(sym, [])
            remaining = qty
            while remaining > 0 and sl:
                consumed = min(sl[0][0], remaining)
                sl[0][0] -= consumed
                remaining -= consumed
                if sl[0][0] <= 0.001:
                    sl.pop(0)

    for sym in set(long_lots.keys()) | set(short_lots.keys()):
        long_remaining = sum(lot[0] for lot in long_lots.get(sym, []))
        short_remaining = sum(lot[0] for lot in short_lots.get(sym, []))
        net = long_remaining - short_remaining
        if abs(net) > 0.001:
            out[sym] = net
    return out


def _broker_qty_per_symbol(api) -> Dict[str, float]:
    """Symbol → signed qty for everything the broker shows on this
    account. Negative = short."""
    out: Dict[str, float] = {}
    try:
        positions = api.list_positions()
    except Exception as exc:
        logger.warning("aggregate_audit: list_positions failed: %s", exc)
        return out
    for p in positions:
        sym = (getattr(p, "symbol", "") or "").upper()
        try:
            out[sym] = float(getattr(p, "qty", 0) or 0)
        except (ValueError, TypeError, AttributeError, KeyError) as _aq_exc:
            # Per-position qty parse fail — the row gets silently
            # dropped from the broker side of the drift comparison.
            # A malformed Alpaca position field is unusual and likely
            # indicates schema drift; WARN so the bug surfaces.
            logger.warning(
                "aggregate_audit per-position qty parse failed "
                "(symbol=%s): %s: %s — this position will be MISSING "
                "from the drift comparison, possibly causing false "
                "drift alerts",
                sym, type(_aq_exc).__name__, _aq_exc,
            )
            continue
    return out


def audit_aggregate_drift(profile_ids: Iterable[int],
                          tolerance: float = _QTY_TOLERANCE) -> Dict:
    """Compare journal-aggregate vs broker-aggregate per Alpaca account.

    Returns:
      {
        'accounts': {acct_id: {symbol: {'journal': float, 'broker': float, 'drift': float}}},
        'drift': [list of dicts where abs(drift) > tolerance],
        'errored': [profile_ids that failed to load],
      }
    """
    from models import build_user_context_from_profile

    # Per-account aggregations
    journal_per_acct: Dict[int, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    api_per_acct: Dict[int, object] = {}
    errored: List[int] = []

    for p_id in profile_ids:
        try:
            ctx = build_user_context_from_profile(p_id)
        except Exception:
            errored.append(p_id)
            continue
        acct = getattr(ctx, "alpaca_account_id", None)
        if not acct:
            continue  # archived / no broker
        if acct not in api_per_acct:
            try:
                api_per_acct[acct] = ctx.get_alpaca_api() if hasattr(ctx, "get_alpaca_api") else ctx.api
            except Exception:
                errored.append(p_id)
                continue
        # Sum this profile's open virtual qty per symbol into the
        # per-account aggregate.
        per_sym = _journal_open_qty_per_symbol(ctx.db_path)
        for sym, qty in per_sym.items():
            journal_per_acct[acct][sym] += qty

    # Broker aggregates
    broker_per_acct: Dict[int, Dict[str, float]] = {}
    for acct, api in api_per_acct.items():
        broker_per_acct[acct] = _broker_qty_per_symbol(api)

    # Compare per (account, symbol). Drift kind classification is
    # by ABSOLUTE qty, not signed: a broker short with no matching
    # journal entry is just as much an "orphan at broker" as a
    # broker long with no matching journal entry.
    accounts: Dict[int, Dict[str, Dict[str, float]]] = {}
    drift: List[Dict] = []
    for acct in set(list(journal_per_acct.keys()) + list(broker_per_acct.keys())):
        symbols = set(journal_per_acct[acct].keys()) | set(broker_per_acct.get(acct, {}).keys())
        accounts[acct] = {}
        for sym in symbols:
            j = round(journal_per_acct[acct].get(sym, 0.0), 4)
            b = round(broker_per_acct.get(acct, {}).get(sym, 0.0), 4)
            d = round(b - j, 4)
            accounts[acct][sym] = {"journal": j, "broker": b, "drift": d}
            if abs(d) > tolerance:
                # broker_orphan: broker holds positions no profile owns
                #   (could be longs or shorts the journal doesn't track)
                # journal_phantom: profiles claim positions the broker
                #   doesn't have
                if abs(b) > abs(j):
                    kind = "broker_orphan"
                else:
                    kind = "journal_phantom"
                drift.append({
                    "account": acct, "symbol": sym,
                    "journal_qty": j, "broker_qty": b, "drift": d,
                    "kind": kind,
                })

    return {"accounts": accounts, "drift": drift, "errored": errored}


# ─────────────────────────────────────────────────────────────────────
# Manual broker-side order detector (D, 2026-06-04)
# ─────────────────────────────────────────────────────────────────────
#
# The atomic-placement contract (A) plus the proactive chain sync (C)
# close the orphan class for orders the SYSTEM places. They don't
# detect orders placed THROUGH the broker by other means — Alpaca.com
# UI clicks, external scripts using the API directly, etc. Those are
# the last orphan path that bypasses every contract this codebase
# enforces. This audit makes them visible by diffing the broker's
# active orders against the union of every profile's journaled
# order_ids per Alpaca account.

# Statuses that count as "active" at the broker for the manual-order
# scan. Filled/canceled/expired orders are excluded — they're
# historical and the reconciler already handles attribution
# of recent fills. We want the currently-actionable surface area.
_BROKER_ACTIVE_STATUSES = frozenset({
    "new", "accepted", "pending_new", "accepted_for_bidding",
    "held", "partially_filled",
    "replaced", "pending_replace",
})

# Grace window (seconds) for excluding very-recent broker orders from
# the manual-order audit. The system submits an order via the broker
# API and then journals it; there's a small race window (submit_order
# returns ms before the INSERT INTO trades commits) where the order
# is visible at the broker but not in any journal yet. Without a grace
# window, the audit fires false-positive alerts on EVERY normal cycle
# that places orders. 60s is well past healthy submit→journal latency
# (typically <1s) but short enough that a genuine manual broker order
# is detected by the next cycle. Documented 2026-06-04 after a
# false-positive at 13:30:49 during the post-reset opening cycle.
_MANUAL_ORDER_GRACE_SECONDS = 60


def _all_journal_order_ids_for_profile(db_path: str) -> set:
    """Every order_id this profile's journal has touched, across all
    statuses. The manual-order audit compares against this union."""
    import sqlite3 as _sqlite3
    out: set = set()
    try:
        conn = _sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        for r in conn.execute(
            "SELECT order_id FROM trades "
            "WHERE order_id IS NOT NULL AND order_id != ''"
        ):
            if r[0]:
                out.add(r[0])
        # protective_*_order_id columns also count — they're the
        # entry-row pointers to live broker protectives. If a journal
        # has them, the broker order is ours.
        try:
            for col in ("protective_stop_order_id",
                        "protective_tp_order_id",
                        "protective_trailing_order_id"):
                for r in conn.execute(
                    f"SELECT {col} FROM trades "
                    f"WHERE {col} IS NOT NULL AND {col} != ''"
                ):
                    if r[0]:
                        out.add(r[0])
        except _sqlite3.OperationalError:
            # Columns don't exist on this minimal schema — skip.
            pass
        conn.close()
    except Exception as exc:
        logger.warning(
            "manual-order audit: failed to read order_ids from %s: %s",
            db_path, exc,
        )
    return out


def _is_within_grace_window(created_at_str: str,
                              now=None,
                              window_seconds: int =
                              _MANUAL_ORDER_GRACE_SECONDS) -> bool:
    """True if the order's broker `created_at` is within the grace
    window from now. Orders inside the window are still in the
    submit→journal race for our own cycle and should be excluded
    from the manual-order audit to avoid false positives."""
    import datetime as _dt
    if not created_at_str:
        return False  # no timestamp → can't establish grace, don't grant it
    try:
        # Alpaca emits ISO 8601 with 'Z' or numeric offset
        s = created_at_str.replace("Z", "+00:00")
        dt = _dt.datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return False  # un-parseable → don't grant grace
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    now = now or _dt.datetime.now(_dt.timezone.utc)
    age = (now - dt).total_seconds()
    return 0 <= age < window_seconds


def _broker_active_orders(api) -> List[Dict]:
    """Pull every currently-active order from the broker. Filters by
    status so only actionable orders surface (filled/canceled history
    is excluded — that's the reconciler's domain).

    Also excludes orders within the grace window (default 60s from
    `created_at`) — those may still be journal-pending from our own
    just-submitted cycle. Without this filter the audit produces
    false-positive alerts on every cycle that places orders."""
    try:
        orders = api.list_orders(status="open", limit=500)
    except Exception as exc:
        logger.warning(
            "manual-order audit: list_orders failed: %s", exc,
        )
        return []
    out: List[Dict] = []
    for o in orders or []:
        status = (getattr(o, "status", "") or "").lower()
        if status not in _BROKER_ACTIVE_STATUSES:
            continue
        oid = getattr(o, "id", None)
        if not oid:
            continue
        created_at = str(getattr(o, "created_at", ""))
        if _is_within_grace_window(created_at):
            # Just-submitted by this system; journal INSERT may not
            # have committed yet. Skip to avoid a race false-positive.
            continue
        out.append({
            "order_id": oid,
            "symbol": (getattr(o, "symbol", "") or "").upper(),
            "side": (getattr(o, "side", "") or "").lower(),
            "qty": float(getattr(o, "qty", 0) or 0),
            "type": (getattr(o, "order_type", None)
                     or getattr(o, "type", "") or "").lower(),
            "status": status,
            "created_at": created_at,
        })
    return out


def audit_manual_broker_orders(profile_ids: Iterable[int]) -> Dict:
    """Detect broker-side orders with no corresponding journal row in
    any profile routing to that Alpaca account.

    Per-account diff:
      live_broker_order_ids on account A
        MINUS
      union(journaled_order_ids for every profile routing to A)
    Anything left is a manual / external order.

    Returns: {
        'accounts': {acct_id: {'total_broker_active': int,
                                'journal_known': int,
                                'manual': [<order dict>, ...]}},
        'manual': [flat list of all manual orders across accounts],
        'errored': [profile_ids that failed to load],
    }
    """
    from models import build_user_context_from_profile

    api_per_acct: Dict[int, object] = {}
    journal_ids_per_acct: Dict[int, set] = defaultdict(set)
    errored: List[int] = []

    for p_id in profile_ids:
        try:
            ctx = build_user_context_from_profile(p_id)
        except Exception:
            errored.append(p_id)
            continue
        acct = getattr(ctx, "alpaca_account_id", None)
        if not acct:
            continue
        if acct not in api_per_acct:
            try:
                api_per_acct[acct] = (
                    ctx.get_alpaca_api()
                    if hasattr(ctx, "get_alpaca_api") else ctx.api
                )
            except Exception:
                errored.append(p_id)
                continue
        journal_ids_per_acct[acct] |= _all_journal_order_ids_for_profile(
            ctx.db_path)

    accounts: Dict[int, Dict] = {}
    manual_flat: List[Dict] = []
    for acct, api in api_per_acct.items():
        broker_orders = _broker_active_orders(api)
        journal_ids = journal_ids_per_acct.get(acct, set())
        manual = [o for o in broker_orders
                   if o["order_id"] not in journal_ids]
        accounts[acct] = {
            "total_broker_active": len(broker_orders),
            "journal_known": len(broker_orders) - len(manual),
            "manual": manual,
        }
        for m in manual:
            manual_flat.append({**m, "account": acct})

    return {
        "accounts": accounts,
        "manual": manual_flat,
        "errored": errored,
    }


def format_manual_orders_summary(audit: Dict) -> str:
    """Human-readable summary for log lines / email."""
    manual = audit.get("manual", [])
    if not manual:
        return ("manual-order audit: 0 manual orders, every broker "
                "order is journaled")
    lines = [f"manual-order audit: {len(manual)} manual broker order(s) "
             "with no journal row"]
    for m in manual:
        lines.append(
            f"  acct{m['account']} {m['symbol']:>8s} {m['side']:>4s} "
            f"qty={m['qty']:>6.0f} type={m['type']:<14s} "
            f"id={m['order_id'][:8]} status={m['status']}"
        )
    return "\n".join(lines)


def format_drift_summary(audit: Dict) -> str:
    """Human-readable summary for log lines / email."""
    drift = audit.get("drift", [])
    if not drift:
        return "aggregate audit: 0 drift items, all accounts in sync"
    lines = [f"aggregate audit: {len(drift)} drift items"]
    for d in drift:
        lines.append(
            f"  acct{d['account']} {d['symbol']:>10s}: "
            f"journal={d['journal_qty']:>+8.2f}  broker={d['broker_qty']:>+8.2f}  "
            f"drift={d['drift']:>+8.2f}  ({d['kind']})"
        )
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# Account-value parity (#165, 2026-05-17)
# ─────────────────────────────────────────────────────────────────────
#
# qty-parity (above) catches mismatched share counts. value-parity
# catches mismatched DOLLAR amounts — different mark prices, missing
# multipliers, stale marks. Quantity audit + value audit + order_id
# pairing together form the 3-tier integrity check between every
# Alpaca account and the virtual profiles routing through it.
#
# Tolerance: max(_VALUE_TOLERANCE_ABS, _VALUE_TOLERANCE_PCT * broker_value).
# The ABS floor handles tiny-account noise (a 0.1% drift on $1K is
# only $1); the PCT term scales with account size so a $1M account
# can absorb $1K of normal snapshot-lag drift.
_VALUE_TOLERANCE_ABS = 50.0      # dollars
_VALUE_TOLERANCE_PCT = 0.001     # 0.1% of broker positions value


def _journal_positions_value(db_path: str, price_fetcher=None) -> float:
    """Sum of market_value across all open virtual positions for ONE
    profile. Uses the same price_fetcher as get_virtual_positions so
    both sides of the comparison are marked consistently."""
    from journal import get_virtual_positions
    try:
        positions = get_virtual_positions(
            db_path=db_path, price_fetcher=price_fetcher,
        )
    except Exception as exc:
        logger.warning(
            "aggregate_audit value-parity: get_virtual_positions failed "
            "for %s: %s: %s", db_path, type(exc).__name__, exc,
        )
        return 0.0
    return sum(float(p.get("market_value", 0) or 0) for p in positions)


def _broker_positions_value(api) -> float:
    """Sum of market_value across the broker's positions on this
    account. Excludes cash deliberately — cash parity is a separate
    invariant (broker cash reflects real deposits; virtual cash is
    bookkeeping from initial_capital, and the two can legitimately
    diverge on shared accounts)."""
    try:
        positions = api.list_positions()
    except Exception as exc:
        logger.warning(
            "aggregate_audit value-parity: list_positions failed: %s",
            exc,
        )
        return 0.0
    total = 0.0
    for p in positions:
        try:
            total += float(getattr(p, "market_value", 0) or 0)
        except (ValueError, TypeError, AttributeError, KeyError) as exc:
            sym = getattr(p, "symbol", "?")
            logger.warning(
                "aggregate_audit value-parity per-position parse failed "
                "(symbol=%s): %s: %s — this position will be MISSING "
                "from the value comparison, possibly causing false drift",
                sym, type(exc).__name__, exc,
            )
    return total


def audit_account_value_parity(
    profile_ids: Iterable[int],
    tolerance_abs: float = _VALUE_TOLERANCE_ABS,
    tolerance_pct: float = _VALUE_TOLERANCE_PCT,
) -> Dict:
    """Compare summed virtual positions value vs broker positions
    value per Alpaca account.

    Returns:
      {
        'accounts': {acct_id: {
            'broker_value': float,
            'journal_value': float,
            'drift': float,        # broker - journal
            'tolerance': float,    # the threshold this account uses
            'profile_ids': [int, ...],
        }},
        'drift': [list of accounts where abs(drift) > tolerance],
        'errored': [profile_ids that failed to load],
      }
    """
    from models import build_user_context_from_profile
    from client import _make_price_fetcher  # shared cache, hot

    by_account: Dict[int, Dict] = defaultdict(
        lambda: {"journal_value": 0.0, "profile_ids": [], "api": None}
    )
    errored: List[int] = []

    for p_id in profile_ids:
        try:
            ctx = build_user_context_from_profile(p_id)
        except Exception:
            errored.append(p_id)
            continue
        acct = getattr(ctx, "alpaca_account_id", None)
        if not acct:
            continue
        if by_account[acct]["api"] is None:
            try:
                api = ctx.get_alpaca_api() if hasattr(
                    ctx, "get_alpaca_api") else ctx.api
                by_account[acct]["api"] = api
            except Exception:
                errored.append(p_id)
                continue
        # Mark each profile's positions using the same fetcher as
        # the broker side will use a moment later — keeps the two
        # sides snapshotted consistently.
        try:
            fetcher = _make_price_fetcher(by_account[acct]["api"])
        except Exception:
            fetcher = None
        v = _journal_positions_value(ctx.db_path, price_fetcher=fetcher)
        by_account[acct]["journal_value"] += v
        by_account[acct]["profile_ids"].append(p_id)

    accounts: Dict[int, Dict] = {}
    drift: List[Dict] = []
    for acct, info in by_account.items():
        api = info["api"]
        if api is None:
            continue
        broker_value = _broker_positions_value(api)
        journal_value = round(info["journal_value"], 2)
        broker_value = round(broker_value, 2)
        d = round(broker_value - journal_value, 2)
        tol = max(tolerance_abs, abs(broker_value) * tolerance_pct)
        row = {
            "account": acct,
            "broker_value": broker_value,
            "journal_value": journal_value,
            "drift": d,
            "tolerance": round(tol, 2),
            "profile_ids": sorted(info["profile_ids"]),
        }
        accounts[acct] = row
        if abs(d) > tol:
            # broker_orphan: broker holds more dollars than profiles claim
            # journal_phantom: profiles claim more dollars than broker holds
            row["kind"] = (
                "broker_value_orphan" if d > 0 else "journal_value_phantom"
            )
            drift.append(row)

    return {"accounts": accounts, "drift": drift, "errored": errored}


def format_value_drift_summary(audit: Dict) -> str:
    drift = audit.get("drift", [])
    if not drift:
        return "value-parity audit: 0 drift items, all account values match"
    lines = [f"value-parity audit: {len(drift)} drift item(s)"]
    for d in drift:
        lines.append(
            f"  acct{d['account']}: broker=${d['broker_value']:>12,.2f}  "
            f"journal=${d['journal_value']:>12,.2f}  "
            f"drift=${d['drift']:>+12,.2f}  "
            f"(tol=${d['tolerance']:,.2f}, {d.get('kind', '?')})"
        )
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# Per-account cash parity (#167, 2026-05-17)
# ─────────────────────────────────────────────────────────────────────
#
# Complement to value-parity: broker_cash for account A should equal
# sum(virtual_cash) across all profiles routing to A — provided the
# user's broker deposit matches the sum of profile initial_capital
# values (the normal setup for the fresh experiment).
#
# If they DON'T match:
#   - Broker received cash flow the journal doesn't know about
#     (dividend credit, fee debit, manual deposit)
#   - OR a trade hit the broker but never made it into the journal
#   - OR initial_capital sum doesn't match the user's actual broker
#     funding (configuration error)
#
# Tolerance same shape as value-parity: max($50, 0.1% × broker_cash).
_CASH_TOLERANCE_ABS = 50.0
_CASH_TOLERANCE_PCT = 0.001


def _broker_cash(api) -> float:
    """Broker's reported cash for this account. 0.0 on failure
    (logged loudly)."""
    try:
        account = api.get_account()
        return float(getattr(account, "cash", 0) or 0)
    except Exception as exc:
        logger.warning(
            "aggregate_audit cash-parity: get_account failed: %s", exc,
        )
        return 0.0


def _journal_cash(db_path: str, initial_capital: float) -> float:
    """One profile's virtual cash (same algebra as
    journal.get_virtual_account_info, but isolated to a single profile)."""
    from journal import get_virtual_account_info
    try:
        info = get_virtual_account_info(
            db_path=db_path, initial_capital=initial_capital,
        )
        return float(info.get("cash", 0) or 0)
    except Exception as exc:
        logger.warning(
            "aggregate_audit cash-parity: get_virtual_account_info "
            "failed for %s: %s: %s", db_path, type(exc).__name__, exc,
        )
        return 0.0


def audit_account_cash_parity(
    profile_ids: Iterable[int],
    tolerance_abs: float = _CASH_TOLERANCE_ABS,
    tolerance_pct: float = _CASH_TOLERANCE_PCT,
) -> Dict:
    """Compare summed virtual cash vs broker cash per Alpaca account.

    Returns:
      {
        'accounts': {acct_id: {
            'broker_cash': float,
            'journal_cash': float,
            'drift': float,            # broker - journal
            'tolerance': float,
            'profile_ids': [int, ...],
            'kind': str (only on drift rows),
        }},
        'drift': [list of drift rows],
        'errored': [profile_ids that failed to load],
      }
    """
    from models import build_user_context_from_profile

    by_account: Dict[int, Dict] = defaultdict(
        lambda: {"journal_cash": 0.0, "profile_ids": [], "api": None}
    )
    errored: List[int] = []

    for p_id in profile_ids:
        try:
            ctx = build_user_context_from_profile(p_id)
        except Exception:
            errored.append(p_id)
            continue
        acct = getattr(ctx, "alpaca_account_id", None)
        if not acct:
            continue
        if by_account[acct]["api"] is None:
            try:
                api = ctx.get_alpaca_api() if hasattr(
                    ctx, "get_alpaca_api") else ctx.api
                by_account[acct]["api"] = api
            except Exception:
                errored.append(p_id)
                continue
        initial_capital = float(
            getattr(ctx, "initial_capital", 0) or 0
        )
        cash = _journal_cash(ctx.db_path, initial_capital)
        by_account[acct]["journal_cash"] += cash
        by_account[acct]["profile_ids"].append(p_id)

    accounts: Dict[int, Dict] = {}
    drift: List[Dict] = []
    for acct, info in by_account.items():
        api = info["api"]
        if api is None:
            continue
        broker_cash = round(_broker_cash(api), 2)
        journal_cash = round(info["journal_cash"], 2)
        d = round(broker_cash - journal_cash, 2)
        tol = max(tolerance_abs, abs(broker_cash) * tolerance_pct)
        row = {
            "account": acct,
            "broker_cash": broker_cash,
            "journal_cash": journal_cash,
            "drift": d,
            "tolerance": round(tol, 2),
            "profile_ids": sorted(info["profile_ids"]),
        }
        accounts[acct] = row
        if abs(d) > tol:
            row["kind"] = (
                "broker_cash_orphan" if d > 0 else "journal_cash_phantom"
            )
            drift.append(row)

    return {"accounts": accounts, "drift": drift, "errored": errored}


# ─────────────────────────────────────────────────────────────────────
# Per-symbol cost-basis parity — DISABLED 2026-06-26 (see the docstring).
# The broker's per-symbol avg_entry_price is an account-level FIFO-net basis
# that is not attributable to any single profile on a shared account, so the
# old broker-vs-journal-avg comparison was a structural false positive. Only
# `Σ qty == broker qty` is broker-verifiable per symbol. The per-profile basis
# from a profile's own fills is authoritative.
# ─────────────────────────────────────────────────────────────────────


def audit_account_basis_parity(profile_ids, *_args, **_kwargs) -> Dict:
    """DISABLED 2026-06-26 — cost basis is NOT broker-verifiable per profile on
    a shared account.

    Each Alpaca conduit account is shared by many virtual-account profiles. The
    broker reports ONE avg_entry_price per symbol — a FIFO-net basis across
    EVERY profile's lots, recomputed after each partial close. That number is
    not attributable to any single profile, nor even to the Σ across profiles
    once any lot has been sold (broker FIFO and per-profile FIFO disagree on
    WHICH lots remain). A profile's TRUE basis is the qty-weighted average of
    its OWN order fills — authoritative, and already broker-sourced per-order
    via the fill_price backfill.

    Comparing the journal's per-profile basis to the account avg produced a
    GUARANTEED false `basis_drift` on every symbol that had a cross-profile
    partial close (e.g. AMZN: profile basis $227.04 from its own fill vs the
    account FIFO-net $236.57), flooding /issues with non-actionable noise. The
    only broker-verifiable per-symbol invariant is `Σ qty == broker qty`
    (`audit_aggregate_drift`); that plus the decomposition identity are the
    real guards. Returns no findings; kept as a no-op so callers
    (issues_collector, audit_runner) and the audit registry are undisturbed.
    """
    return {"accounts": {}, "drift": [], "errored": []}


def format_cash_drift_summary(audit: Dict) -> str:
    drift = audit.get("drift", [])
    if not drift:
        return "cash-parity audit: 0 drift items, broker cash matches journal sums"
    lines = [f"cash-parity audit: {len(drift)} drift item(s)"]
    for d in drift:
        lines.append(
            f"  acct{d['account']}: broker_cash=${d['broker_cash']:>12,.2f}  "
            f"journal_cash=${d['journal_cash']:>12,.2f}  "
            f"drift=${d['drift']:>+12,.2f}  "
            f"(tol=${d['tolerance']:,.2f}, {d.get('kind', '?')})"
        )
    return "\n".join(lines)


