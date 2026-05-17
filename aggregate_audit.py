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
    audit = audit_aggregate_drift(profile_ids=range(1,12))
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
