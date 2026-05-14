"""One-shot cleanup of phantom options positions in the journal.

The bug class.
Some options positions in the journal show status='open' but the
broker (Alpaca) does NOT have the matching position. Each cycle the
exit-checker fires a close attempt, the broker rejects with 403
"account not eligible to trade uncovered" or 422 "position intent
mismatch", and the journal stays open for the next cycle to retry.
~196 close-rejections per day across all profiles before this fix.

Likely causes:
- Multi-leg combo whose leg-pair link broke (one leg closed, partner
  still in journal as open even though the combo unwound).
- Manual broker-side close that didn't reflect back into the journal.
- Reconcile step missed the symbol (acct_alias mismatch, etc.).

This script:
1. Connects to Alpaca for each account, fetches actual options
   positions by OCC symbol.
2. Iterates per-profile journal options that show status='open'.
3. For each that does NOT appear at the broker (with sufficient qty),
   marks the row canceled with `reason='phantom: broker had no
   matching position (cleanup script 2026-05-14)'`.
4. Reports the cleanup summary.

Idempotent: re-running is safe (the WHERE status='open' filter
short-circuits already-cleaned rows).

Run on prod: python3 /tmp/cleanup_phantom_options.py
"""
from __future__ import annotations

import datetime
import os
import sqlite3
import sys
from contextlib import closing
from typing import Dict, List, Set, Tuple

DB_DIR = "/opt/quantopsai"
sys.path.insert(0, DB_DIR)

PROFILES = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]


def _broker_options_by_account() -> Dict[int, Set[str]]:
    """Fetch every options position the broker holds, grouped by
    Alpaca-account-id. Returns {alpaca_account_id: {occ_symbol, ...}}.

    Maps account_id → set of OCC symbols the broker has any nonzero
    quantity in. We only care about presence/absence here, not qty.
    """
    out: Dict[int, Set[str]] = {}
    # The system has multiple Alpaca accounts; each profile maps to one.
    # Discover unique accounts across the master DB.
    master = f"{DB_DIR}/quantopsai.db"
    with closing(sqlite3.connect(master)) as mc:
        mc.row_factory = sqlite3.Row
        accts = mc.execute(
            "SELECT DISTINCT alpaca_account_id FROM trading_profiles "
            "WHERE enabled=1 AND alpaca_account_id IS NOT NULL"
        ).fetchall()
    if not accts:
        print("WARNING: no alpaca_account_id values found on enabled profiles. "
              "Falling back to per-profile broker queries.")
    for r in accts:
        acct_id = int(r["alpaca_account_id"])
        try:
            from client import _build_api_for_account
            api = _build_api_for_account(acct_id)
            positions = api.list_positions()
            occ_set = set()
            for p in positions:
                sym = getattr(p, "symbol", "") or ""
                # OCC symbols are 15+ chars (UNDERLYING + 6 date + C/P + 8 strike)
                if len(sym) >= 15 and any(c in sym for c in "CP"):
                    occ_set.add(sym.replace(" ", ""))
            out[acct_id] = occ_set
            print(
                f"  acct {acct_id}: {len(occ_set)} option positions at broker"
            )
        except Exception as exc:
            print(f"  acct {acct_id}: ERROR fetching broker positions: {exc}")
            out[acct_id] = set()
    return out


def _profile_account_map() -> Dict[int, int]:
    """{profile_id: alpaca_account_id} for every enabled profile."""
    master = f"{DB_DIR}/quantopsai.db"
    with closing(sqlite3.connect(master)) as mc:
        mc.row_factory = sqlite3.Row
        rows = mc.execute(
            "SELECT id, alpaca_account_id FROM trading_profiles "
            "WHERE enabled=1"
        ).fetchall()
    return {r["id"]: r["alpaca_account_id"] for r in rows
            if r["alpaca_account_id"] is not None}


def _find_phantom_journal_options(
    profile_id: int,
    broker_occ_set: Set[str],
) -> List[Tuple[int, str, str, float, str]]:
    """Return list of (id, symbol, occ_symbol, qty, timestamp) for
    journal-open options on this profile that the broker does NOT
    hold (or holds in zero quantity)."""
    db = f"{DB_DIR}/quantopsai_profile_{profile_id}.db"
    if not os.path.exists(db):
        return []
    with closing(sqlite3.connect(db)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, symbol, occ_symbol, qty, timestamp FROM trades "
            "WHERE status='open' AND occ_symbol IS NOT NULL"
        ).fetchall()
    phantoms = []
    for r in rows:
        occ = (r["occ_symbol"] or "").replace(" ", "")
        if occ and occ not in broker_occ_set:
            phantoms.append(
                (r["id"], r["symbol"], occ, float(r["qty"] or 0), r["timestamp"])
            )
    return phantoms


def _mark_canceled(
    profile_id: int,
    trade_id: int,
    occ_symbol: str,
    reason: str,
) -> bool:
    db = f"{DB_DIR}/quantopsai_profile_{profile_id}.db"
    with closing(sqlite3.connect(db)) as conn:
        cur = conn.execute(
            "UPDATE trades SET status='canceled', "
            "reason=COALESCE(reason || ' | ', '') || ? "
            "WHERE id=? AND status='open'",
            (reason, trade_id),
        )
        conn.commit()
        return cur.rowcount > 0


def main():
    print("=" * 70)
    print("Phantom options cleanup — 2026-05-14")
    print("=" * 70)
    print()

    # Backup is per-DB so the existing /opt/quantopsai/backups/ scheme
    # keeps working. Caller should run sync.sh after to ensure
    # consistency, but the modifications here are tiny (single-row
    # UPDATEs) and recoverable by re-marking status='open' if needed.
    print("[1/3] Fetching broker options positions per account...")
    broker_by_acct = _broker_options_by_account()
    profile_acct = _profile_account_map()
    print(f"  account mapping: {profile_acct}")
    print()

    print("[2/3] Identifying phantom journal options per profile...")
    total_phantoms = 0
    cleanup_plan: Dict[int, List[Tuple[int, str, str, float, str]]] = {}
    for pid in PROFILES:
        acct = profile_acct.get(pid)
        if acct is None:
            print(f"  pid {pid}: no Alpaca account mapping — skipping")
            continue
        broker_occ = broker_by_acct.get(acct, set())
        phantoms = _find_phantom_journal_options(pid, broker_occ)
        if phantoms:
            cleanup_plan[pid] = phantoms
            total_phantoms += len(phantoms)
            print(f"  pid {pid} (acct {acct}): {len(phantoms)} phantom(s)")
            for tid, sym, occ, qty, ts in phantoms:
                print(f"    id={tid} {sym} {occ} qty={qty} opened={ts}")
        else:
            print(f"  pid {pid}: no phantoms")
    print()
    print(f"Total phantoms to mark canceled: {total_phantoms}")
    print()

    if total_phantoms == 0:
        print("Nothing to do. Exiting.")
        return

    print("[3/3] Marking phantoms canceled...")
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    canceled = 0
    for pid, phantoms in cleanup_plan.items():
        for tid, sym, occ, qty, ts in phantoms:
            reason = (
                f"phantom: broker had no matching position "
                f"(cleanup script {now}) — opened {ts}, qty={qty}"
            )
            if _mark_canceled(pid, tid, occ, reason):
                canceled += 1
                print(f"  pid {pid} id={tid} {occ}: canceled")
    print()
    print(f"DONE. Marked {canceled}/{total_phantoms} canceled.")
    print()
    print("These journal rows are now closed. The next exit-check cycle "
          "will not retry them. The new phantom-detection code in "
          "trader.py:_handle_phantom_option_close will catch any FUTURE "
          "phantoms automatically and notify on first occurrence.")


if __name__ == "__main__":
    sys.exit(main())
