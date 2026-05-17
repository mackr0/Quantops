"""Deterministic auto-reconciler for aggregate journal-vs-broker drift.

Resolves the 123 drift items surfaced by `aggregate_audit.audit_aggregate_drift`
on the /issues page. Per the "no errors silent or otherwise" + "AI-driven,
no human-in-the-loop" rules:

  - broker_orphan (broker has X, journal has 0):
      Insert a journal row in the FIRST enabled profile sharing that
      Alpaca account. Records side ('buy' for positive qty, 'short'
      for negative), qty = |broker_qty|, price = current market mark,
      signal_type = 'AUTO_RECONCILE', status='open', with a reason
      string naming this script + the drift snapshot. The profile's
      virtual ledger now reflects reality.

  - journal_phantom (journal has X, broker has 0):
      Mark every contributing open journal row status='auto_reconciled_phantom_close'
      with pnl=0 (we don't know the real outcome; presumably the
      position was closed via a path that didn't update the journal —
      e.g., a manual broker close, a missed _task_update_fills cycle).

Default is DRY-RUN. Pass --apply to actually write. Every action is
logged at INFO with the full before/after state.

Run on prod:
    cd /opt/quantopsai && source .env && \\
    /opt/quantopsai/venv/bin/python reconcile_aggregate_drift.py            # dry-run
    /opt/quantopsai/venv/bin/python reconcile_aggregate_drift.py --apply    # actually write
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


def _profiles_sharing_account(
    account_id: int, user_id: int,
) -> List[Dict]:
    """Return the enabled profiles that route to this Alpaca account,
    ordered by id ASC so the "first" profile is deterministic."""
    from models import get_user_profiles
    profs = [
        p for p in get_user_profiles(user_id)
        if (p.get("enabled")
            and p.get("alpaca_account_id") == account_id)
    ]
    return sorted(profs, key=lambda p: p["id"])


def _find_opening_orders_for_position(
    api, symbol: str, broker_qty: float,
) -> List[Dict]:
    """Return Alpaca orders that opened the current broker position.

    Strategy: pull recent filled orders for `symbol`, filter to the
    side that opens (buy for long, sell/sell_short for short), order
    newest-first, accumulate filled_qty until it reaches |broker_qty|.
    Returns a list of dicts with keys:
        order_id, filled_avg_price, filled_at, filled_qty

    For OCC option symbols where the order is a combo (Alpaca returns
    the combo id; per-leg fills live on order.legs[i]), walks the legs
    to find the one matching our OCC.

    Returns [] when Alpaca's order history doesn't go far enough back
    or the orders can't be reconstructed — caller falls back to the
    synthetic "use current mark + lowest-id profile" path.
    """
    if api is None:
        return []
    # Paginate. Alpaca's list_orders caps at limit=500/call; for a
    # trader that submits ~20 orders/day, that's ~3 weeks of history
    # per page. Walk back in 500-order pages using `until=<earliest
    # submitted_at so far>` until we cover the position OR exhaust
    # available history. Cap total pages at 8 (= 4000 orders) so we
    # don't spin forever on a degenerate symbol.
    is_long = broker_qty > 0
    need_qty = abs(broker_qty)
    is_occ = (len(symbol) > 6 and any(c.isdigit() for c in symbol[1:7]))
    opening_sides = (("buy",) if is_long
                     else ("sell", "sell_short"))

    all_orders: List = []
    until_param: Optional[str] = None
    MAX_PAGES = 8
    PAGE_SIZE = 500
    for _page in range(MAX_PAGES):
        try:
            kwargs = {"status": "all", "symbols": [symbol],
                      "limit": PAGE_SIZE, "nested": True,
                      "direction": "desc"}
            if until_param:
                kwargs["until"] = until_param
            page = api.list_orders(**kwargs)
        except Exception as exc:
            log.warning(
                "  list_orders(%s, page=%d) failed: %s: %s — using "
                "whatever %d orders we already pulled",
                symbol, _page, type(exc).__name__, exc, len(all_orders),
            )
            break
        if not page:
            break
        all_orders.extend(page)
        # Heuristic short-circuit: if we already have enough opening-
        # side filled qty to cover the position, stop paginating.
        opening_filled_qty = sum(
            float(getattr(o, "filled_qty", 0) or 0)
            for o in all_orders
            if (getattr(o, "side", "") in opening_sides
                and float(getattr(o, "filled_qty", 0) or 0) > 0)
        )
        if opening_filled_qty >= need_qty - 0.001:
            break
        # Last page returned full PAGE_SIZE → more history may exist.
        # Next iteration pulls everything submitted BEFORE the oldest
        # in this page.
        if len(page) < PAGE_SIZE:
            break
        oldest = min(
            (getattr(o, "submitted_at", None) for o in page
             if getattr(o, "submitted_at", None)),
            default=None,
        )
        if not oldest:
            break
        until_param = str(oldest)

    # Sort newest fills first — most recent opens are most likely to
    # be the live position (older fills may have been closed and
    # re-opened).
    def _ts_of(o):
        ts = (getattr(o, "filled_at", None)
              or getattr(o, "submitted_at", None) or "")
        return str(ts)

    candidates = [
        o for o in all_orders
        if (getattr(o, "side", "") in opening_sides
            and float(getattr(o, "filled_qty", 0) or 0) > 0)
    ]
    candidates.sort(key=_ts_of, reverse=True)

    matched: List[Dict] = []
    accumulated = 0.0
    for o in candidates:
        if accumulated >= need_qty - 0.001:
            break
        fq = float(getattr(o, "filled_qty", 0) or 0)
        if fq <= 0:
            continue
        # For OCC option orders, the order's symbol may be the OCC
        # itself OR the combo wrapper; find the leg matching ours.
        leg_price = None
        if is_occ:
            for leg in (getattr(o, "legs", None) or []):
                if getattr(leg, "symbol", "") == symbol:
                    lap = getattr(leg, "filled_avg_price", None)
                    if lap is not None and float(lap) > 0:
                        leg_price = float(lap)
                    break
            # If no leg matched, the order itself IS for this OCC
            if leg_price is None and getattr(o, "symbol", "") == symbol:
                fap = getattr(o, "filled_avg_price", None)
                if fap is not None and float(fap) > 0:
                    leg_price = float(fap)
        else:
            fap = getattr(o, "filled_avg_price", None)
            if fap is not None and float(fap) > 0:
                leg_price = float(fap)

        if leg_price is None or leg_price <= 0:
            continue
        matched.append({
            "order_id": getattr(o, "id", "?"),
            "filled_avg_price": leg_price,
            "filled_at": getattr(o, "filled_at", None) or "",
            "filled_qty": fq,
        })
        accumulated += fq

    if accumulated < need_qty - 0.001:
        # Didn't find enough history to cover the broker position.
        # Caller can decide whether to use what we found or fall
        # back to synthetic.
        log.warning(
            "  order-history for %s covers only %.2f of %.2f qty "
            "(needed by broker) — partial reconstruction",
            symbol, accumulated, need_qty,
        )
    return matched


def _find_owning_profile(
    order_ids: List[str], profiles: List[Dict],
) -> Optional[Dict]:
    """Search every profile's trades table for any of `order_ids`.
    Returns the FIRST profile (by id) that has a matching journal
    row — that profile most likely owns the position.

    Returns None when no profile DB has any matching row; caller
    falls back to the lowest-id-profile synthetic attribution.
    """
    if not order_ids:
        return None
    placeholders = ",".join(["?"] * len(order_ids))
    for profile in sorted(profiles, key=lambda p: p["id"]):
        db_path = f"quantopsai_profile_{profile['id']}.db"
        if not os.path.exists(db_path):
            continue
        try:
            with closing(sqlite3.connect(db_path)) as conn:
                row = conn.execute(
                    f"SELECT COUNT(*) FROM trades WHERE order_id IN "
                    f"({placeholders})",
                    order_ids,
                ).fetchone()
                if row and row[0] > 0:
                    return profile
        except sqlite3.Error:
            continue
    return None


_BROKER_POSITIONS_CACHE: Dict[str, Dict[str, Dict]] = {}


def _broker_position_lookup(api, acct_id) -> Dict[str, Dict]:
    """Return {symbol: {'qty': float, 'avg_entry_price': float,
    'market_value': float, 'side': str}} for every position the
    broker shows on this account. Cached per-account so we don't
    re-list_positions for every drift item.

    The `avg_entry_price` from `list_positions` is the
    AUTHORITATIVE source for entry price — Alpaca tracks it
    independently of order history. Better than walking
    `list_orders` (which may not reach back far enough).
    """
    if acct_id in _BROKER_POSITIONS_CACHE:
        return _BROKER_POSITIONS_CACHE[acct_id]
    out: Dict[str, Dict] = {}
    if api is None:
        return out
    try:
        positions = api.list_positions()
    except Exception as exc:
        log.warning(
            "  list_positions(acct=%s) failed: %s: %s — entry-price "
            "lookup will fall back to current mark",
            acct_id, type(exc).__name__, exc,
        )
        return out
    for p in positions:
        sym = (getattr(p, "symbol", "") or "").upper()
        if not sym:
            continue
        try:
            aep = getattr(p, "avg_entry_price", None)
            out[sym] = {
                "qty": float(getattr(p, "qty", 0) or 0),
                "avg_entry_price": float(aep) if aep is not None else None,
                "market_value": float(getattr(p, "market_value", 0) or 0),
                "side": getattr(p, "side", "") or "",
            }
        except (ValueError, TypeError, AttributeError):
            continue
    _BROKER_POSITIONS_CACHE[acct_id] = out
    return out


def _current_mark(api, symbol: str, qty: float) -> Optional[float]:
    """Best-effort current price for the symbol. Returns None when
    Alpaca can't price it (delisted, off-hours stale, etc.)."""
    try:
        if len(symbol) > 6 and any(c.isdigit() for c in symbol[1:7]):
            # OCC option symbol — use the options snapshot path
            from client import _fetch_option_premium
            side = "buy" if qty >= 0 else "sell"
            p = _fetch_option_premium(symbol, side=side)
            return float(p) if p and p > 0 else None
        # Stock — last trade
        t = api.get_latest_trade(symbol)
        return float(t.price) if t and getattr(t, "price", None) else None
    except Exception as exc:
        log.warning(
            "  could not price %s for reconciliation: %s: %s",
            symbol, type(exc).__name__, exc,
        )
        return None


def _backfill_broker_orphan(
    fallback_profile: Dict, all_profiles_in_acct: List[Dict],
    account_id, symbol: str, broker_qty: float, apply: bool,
) -> bool:
    """Insert a journal row reflecting the existing broker position.

    Two-tier strategy for HISTORY accuracy:
      Tier 1 (preferred): walk Alpaca order history for `symbol`,
        find the actual opening fill(s) — real entry price, real
        fill timestamp, real order_id. Then cross-reference all
        profile DBs by order_id to attribute correctly.
      Tier 2 (fallback): if order history is empty or doesn't reach
        far enough back, use the current Alpaca mark as entry +
        attribute to `fallback_profile` (lowest-id profile sharing
        the account). Mark the row with `attribution='synthetic'`
        in the reason string so the audit trail is honest about
        what was guessed.

    Returns True iff a write would happen (under dry-run or apply).
    """
    side = "buy" if broker_qty > 0 else "short"
    qty = abs(broker_qty)

    # Build api / ctx once. Fall back to None if test/broken.
    try:
        from client import get_api
        from models import build_user_context_from_profile
        ctx = build_user_context_from_profile(fallback_profile["id"])
        api = get_api(ctx)
    except Exception as exc:
        log.warning(
            "  ctx/api build failed for profile %d (%s: %s) — "
            "running with api=None (tier-2 mark fallback only)",
            fallback_profile["id"], type(exc).__name__, exc,
        )
        api = None

    # PRIMARY: real entry price from `list_positions.avg_entry_price`
    # — authoritative, set by Alpaca, doesn't depend on order history
    # being intact. This is what makes the reconciled rows TRULY
    # accurate for entry price.
    broker_positions = _broker_position_lookup(api, account_id)
    broker_row = broker_positions.get(symbol.upper(), {})
    avg_entry = broker_row.get("avg_entry_price")

    # SECONDARY: walk list_orders for order_id (cross-profile
    # attribution) + actual fill timestamp.
    orders = _find_opening_orders_for_position(api, symbol, broker_qty)
    if orders:
        first_filled = sorted(
            (o["filled_at"] for o in orders if o["filled_at"]),
            key=str,
        )
        first_ts = first_filled[0] if first_filled else None
        owning = _find_owning_profile(
            [o["order_id"] for o in orders],
            all_profiles_in_acct,
        )
        target_profile = owning or fallback_profile
        if owning and avg_entry is not None:
            attribution = "broker-avg-entry+journal-match"
        elif owning:
            # We have journal match but no broker avg_entry; use
            # order weighted price.
            attribution = "order-history+journal-match"
        elif avg_entry is not None:
            attribution = "broker-avg-entry+lowest-id-fallback"
        else:
            attribution = "order-history-but-no-journal-match"
        if avg_entry is not None:
            entry_price = avg_entry
        else:
            # Compute weighted from order fills
            total_qty = sum(o["filled_qty"] for o in orders)
            entry_price = (
                sum(o["filled_qty"] * o["filled_avg_price"] for o in orders)
                / total_qty
            ) if total_qty > 0 else 0.0
        entry_ts = (str(first_ts) if first_ts
                    else datetime.now(timezone.utc).isoformat(timespec="seconds"))
        first_order_id = orders[0]["order_id"]
        history_note = (
            f"avg_entry_from_broker={avg_entry}; "
            f"orders_matched={len(orders)}; first_fill={entry_ts}"
        )
    elif avg_entry is not None:
        # No order history but broker DOES have an avg_entry_price.
        # Use it as the truth. Attribution falls back to lowest-id.
        target_profile = fallback_profile
        entry_price = avg_entry
        entry_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        first_order_id = "auto_reconcile"
        attribution = "broker-avg-entry+lowest-id-no-orders"
        history_note = (
            f"no list_orders history but broker tracks "
            f"avg_entry_price=${avg_entry:.4f}; using that as the "
            f"authoritative entry. Attribution = lowest-id profile."
        )
    else:
        # No history AND no broker avg_entry — last-resort current mark.
        target_profile = fallback_profile
        entry_price = _current_mark(api, symbol, broker_qty) or 0.0
        entry_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        first_order_id = "auto_reconcile"
        attribution = "synthetic-no-history"
        history_note = (
            "no Alpaca order history AND no broker avg_entry_price "
            "available; entry price = current mark; attribution = "
            "lowest-id profile sharing the account"
        )

    db_path = f"quantopsai_profile_{target_profile['id']}.db"
    if not os.path.exists(db_path):
        log.warning(
            "  SKIP broker_orphan %s acct%s: profile %d db missing (%s)",
            symbol, account_id, target_profile["id"], db_path,
        )
        return False
    if entry_price <= 0:
        log.warning(
            "  SKIP broker_orphan %s acct%s profile %d: cannot price "
            "(history empty AND current mark unavailable) — leaving "
            "drift in place rather than writing price=0",
            symbol, account_id, target_profile["id"],
        )
        return False

    reason = (
        f"AUTO_RECONCILE backfill of broker_orphan {symbol} "
        f"qty={broker_qty:+.4f} into profile {target_profile['id']} "
        f"({target_profile.get('name','?')}). "
        f"Source: reconcile_aggregate_drift on "
        f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}. "
        f"Attribution: {attribution}. "
        f"History: {history_note}. "
        f"Original drift root cause: residue from May 11 cross-"
        f"profile short-overshoot or a pre-fix multileg combo-net "
        f"write."
    )
    # OCC option symbol detection + underlying extraction.
    # Convention in this codebase: trades.symbol = UNDERLYING ticker
    # (e.g. "DOW"); trades.occ_symbol = full OCC payload (e.g.
    # "DOW260618C00040000"). get_account_info downstream sends
    # `symbol` to Alpaca's market-data API, which rejects OCC
    # payloads as 'invalid symbol'. Caught 2026-05-17 — broke the
    # dashboard for 5 profiles after the AUTO_RECONCILE writes.
    occ = None
    write_symbol = symbol
    if (len(symbol) > 6 and any(c.isdigit() for c in symbol[1:7])):
        occ = symbol
        # Strip the trailing 6-digit date + C/P + 8-digit strike to
        # recover the underlying. e.g. "DOW260618C00040000" → "DOW".
        write_symbol = re.sub(r"\d{6}[CP]\d{8}$", "", symbol)
        if not write_symbol:
            # Pathological OCC with no recoverable underlying; fall
            # back to a sentinel that won't be sent to Alpaca.
            write_symbol = "_OCC_NO_UNDERLYING"

    log.info(
        "  %s broker_orphan: profile %d (%s) ← %s %s qty=%.4f @ "
        "$%.4f [%s, order_id=%s]",
        "WRITE" if apply else "DRY",
        target_profile["id"], target_profile.get("name", "?"),
        side.upper(), symbol, qty, entry_price, attribution,
        first_order_id,
    )
    if not apply:
        return True

    try:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                "INSERT INTO trades (timestamp, symbol, side, qty, price, "
                "                     fill_price, signal_type, reason, "
                "                     status, occ_symbol, order_id) "
                "VALUES (?, ?, ?, ?, ?, ?, 'AUTO_RECONCILE', ?, 'open', ?, ?)",
                (entry_ts, write_symbol, side, qty, entry_price, entry_price,
                 reason, occ, first_order_id),
            )
            conn.commit()
    except sqlite3.Error as exc:
        log.error(
            "  WRITE FAILED for broker_orphan %s profile %d: %s: %s",
            symbol, target_profile["id"], type(exc).__name__, exc,
        )
        return False
    return True


def _close_journal_phantom(
    profiles: List[Dict], account_id, symbol: str,
    apply: bool,
) -> int:
    """For each profile sharing this account, mark any open journal
    rows for `symbol` as `status='auto_reconciled_phantom_close'`.
    Returns count of rows that would be (or were) marked."""
    n_marked = 0
    for profile in profiles:
        db_path = f"quantopsai_profile_{profile['id']}.db"
        if not os.path.exists(db_path):
            continue
        try:
            with closing(sqlite3.connect(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                # Match ALL open rows for the symbol (buy/short
                # entries AND sell-to-open OCC rows — multileg short
                # legs use side='sell' with occ_symbol set). Closing
                # them all is correct for the journal_phantom case:
                # broker has 0, so whatever's "open" in journal is
                # the phantom.
                rows = conn.execute(
                    "SELECT id, symbol, side, qty, price FROM trades "
                    "WHERE (symbol = ? OR occ_symbol = ?) "
                    "  AND COALESCE(status,'open') = 'open'",
                    (symbol, symbol),
                ).fetchall()
                for r in rows:
                    log.info(
                        "  %s journal_phantom: profile %d row #%d "
                        "%s %s qty=%.4f price=%.4f → auto_reconciled_phantom_close",
                        "WRITE" if apply else "DRY",
                        profile["id"], r["id"], r["side"].upper(),
                        symbol, r["qty"], r["price"] or 0,
                    )
                    if apply:
                        conn.execute(
                            "UPDATE trades SET status = "
                            "'auto_reconciled_phantom_close', "
                            "pnl = 0 WHERE id = ?",
                            (r["id"],),
                        )
                    n_marked += 1
                if apply:
                    conn.commit()
        except sqlite3.Error as exc:
            log.error(
                "  phantom-close DB error for profile %d %s: %s: %s",
                profile["id"], symbol, type(exc).__name__, exc,
            )
    return n_marked


def reconcile(apply: bool = False, user_id: int = 1) -> Dict[str, int]:
    """Run the deterministic reconciler. Returns a counters dict."""
    from aggregate_audit import audit_aggregate_drift

    audit = audit_aggregate_drift(profile_ids=range(1, 12))
    drift = audit.get("drift", [])
    if not drift:
        log.info("No drift — nothing to reconcile.")
        return {"broker_orphan_backfilled": 0, "journal_phantom_closed": 0,
                "skipped": 0}

    counters = {"broker_orphan_backfilled": 0, "journal_phantom_closed": 0,
                "skipped": 0}

    log.info("=" * 60)
    log.info("RECONCILE: %d drift items (apply=%s)", len(drift), apply)
    log.info("=" * 60)

    for d in drift:
        sym = d["symbol"]
        acct = d["account"]
        kind = d["kind"]
        profiles = _profiles_sharing_account(acct, user_id)
        if not profiles:
            log.warning(
                "  SKIP %s acct%s: no enabled profile shares this "
                "account in user %d's roster",
                sym, acct, user_id,
            )
            counters["skipped"] += 1
            continue

        if kind == "broker_orphan":
            ok = _backfill_broker_orphan(
                profiles[0], profiles, acct, sym, d["broker_qty"], apply,
            )
            if ok:
                counters["broker_orphan_backfilled"] += 1
            else:
                counters["skipped"] += 1
        elif kind == "journal_phantom":
            n = _close_journal_phantom(profiles, acct, sym, apply)
            counters["journal_phantom_closed"] += n
            if n == 0:
                counters["skipped"] += 1
        else:
            log.warning(
                "  SKIP %s acct%s: unknown kind=%r — no action",
                sym, acct, kind,
            )
            counters["skipped"] += 1

    log.info("=" * 60)
    log.info(
        "DONE (apply=%s): backfilled=%d phantom_closed=%d skipped=%d",
        apply,
        counters["broker_orphan_backfilled"],
        counters["journal_phantom_closed"],
        counters["skipped"],
    )
    log.info("=" * 60)
    return counters


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually write changes. Default is dry-run.",
    )
    parser.add_argument(
        "--user-id", type=int, default=1,
        help="User whose profiles to reconcile (default 1).",
    )
    args = parser.parse_args()
    counters = reconcile(apply=args.apply, user_id=args.user_id)
    return 0 if counters["skipped"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
