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
    profile: Dict, account_id, symbol: str, broker_qty: float,
    apply: bool,
) -> bool:
    """Insert a journal row in `profile` reflecting the existing
    broker position. Returns True iff a write would happen."""
    side = "buy" if broker_qty > 0 else "short"
    qty = abs(broker_qty)
    db_path = f"quantopsai_profile_{profile['id']}.db"
    if not os.path.exists(db_path):
        log.warning(
            "  SKIP broker_orphan %s acct%s: profile %d db missing (%s)",
            symbol, account_id, profile["id"], db_path,
        )
        return False

    try:
        from client import get_api
        from models import build_user_context_from_profile
        ctx = build_user_context_from_profile(profile["id"])
        api = get_api(ctx)
    except Exception as exc:
        # Test contexts or broken installs — let _current_mark
        # see api=None and decide what to do (it's already
        # exception-tolerant; will return None and we'll skip).
        log.warning(
            "  ctx/api build failed for profile %d (%s: %s) — "
            "_current_mark will run with api=None",
            profile["id"], type(exc).__name__, exc,
        )
        api = None
    price = _current_mark(api, symbol, broker_qty)
    if price is None or price <= 0:
        log.warning(
            "  SKIP broker_orphan %s acct%s profile %d: cannot get "
            "current mark — leave drift in place rather than writing "
            "an unpriced row (would tip back to invisibility)",
            symbol, account_id, profile["id"],
        )
        return False

    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    reason = (
        f"AUTO_RECONCILE backfill of broker_orphan {symbol} qty={broker_qty:+.4f} "
        f"into profile {profile['id']} ({profile.get('name','?')}). "
        f"Detected by reconcile_aggregate_drift on {ts}. "
        f"The position exists on Alpaca acct {account_id} but was "
        f"missing from any profile's journal — likely residue from "
        f"the May 11 cross-profile short-overshoot incident or a "
        f"pre-fix multileg combo-net write. Mark price ${price:.4f} "
        f"used as both entry and current; P&L tracking starts now."
    )

    occ = symbol if (len(symbol) > 6
                     and any(c.isdigit() for c in symbol[1:7])) else None

    log.info(
        "  %s broker_orphan: profile %d (%s) ← %s %s qty=%.4f @ $%.4f",
        "WRITE" if apply else "DRY",
        profile["id"], profile.get("name", "?"),
        side.upper(), symbol, qty, price,
    )
    if not apply:
        return True

    try:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                "INSERT INTO trades (timestamp, symbol, side, qty, price, "
                "                     fill_price, signal_type, reason, "
                "                     status, occ_symbol, order_id) "
                "VALUES (?, ?, ?, ?, ?, ?, 'AUTO_RECONCILE', ?, 'open', ?, "
                "        'auto_reconcile')",
                (ts, symbol, side, qty, price, price, reason, occ),
            )
            conn.commit()
    except sqlite3.Error as exc:
        log.error(
            "  WRITE FAILED for broker_orphan %s profile %d: %s: %s",
            symbol, profile["id"], type(exc).__name__, exc,
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
                rows = conn.execute(
                    "SELECT id, symbol, side, qty, price FROM trades "
                    "WHERE (symbol = ? OR occ_symbol = ?) "
                    "  AND COALESCE(status,'open') = 'open' "
                    "  AND side IN ('buy', 'short')",
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
                profiles[0], acct, sym, d["broker_qty"], apply,
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
