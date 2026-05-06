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
    Long positions add positive qty; shorts subtract."""
    from journal import get_virtual_positions
    out: Dict[str, float] = {}
    try:
        positions = get_virtual_positions(db_path=db_path)
    except Exception:
        return out
    for p in positions:
        sym = (p.get("symbol") or "").upper()
        out[sym] = out.get(sym, 0.0) + float(p.get("qty") or 0)
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
        except Exception:
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
