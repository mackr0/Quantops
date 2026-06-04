"""Safety-net cleanup: orphan trailing-stop fills accumulated May 28 - Jun 3 2026.

Background
----------
Between when the protective-journaling fix landed (b77e4d5, 2026-05-21)
and when the replace-chain walk shipped (this commit, 2026-06-04),
~40 trailing-stop fills across 10 profiles fell through the reconciler's
pending_protective UPDATE path. Alpaca silently REPLACES trailing-stop
orders as the trail bumps; the fill arrived under a post-replacement
order_id that didn't match any journaled pending_protective row, and the
safety net halted each profile.

The structural fix (_walk_replace_chain_forward in
reconcile_journal_to_broker.py) makes the NEXT reconcile pass auto-clear
the halts for any orphan whose entry-row protective_trailing_order_id
still points into a walkable chain ending at the filled order. This
script is the SAFETY NET for cases where the auto-clear can't complete:
broken chain links, stale entry pointers, or operator preference to
clear immediately without waiting for the next pass.

What it does
------------
For each profile that has an unresolved reconciler_synthesis_halt
audit_alert:
  1. Parse the `backfill_sell:` lines from the alert detail (symbol,
     qty, terminal sell_order_id, fill price).
  2. For each orphan, find the entry-side open BUY row (matched by
     symbol + qty) and the newest pending_protective row (matched by
     symbol + qty + status='pending_protective').
  3. Mirror what reconcile_with_ctx + the chain walk WOULD do:
       - flip pending_protective row to status='closed' at fill price
       - close the entry BUY with realized pnl
       - mark any OTHER pending_protective rows for the same symbol+qty
         as canceled (stale siblings from prior replace cycles)
  4. Idempotent: if the entry is already closed OR no matching pending
     row exists, skip the orphan.
  5. Verify-or-refuse: if the journal state doesn't match the expected
     shape (symbol/qty/side), refuse to act rather than mutate
     unfamiliar data.

Usage
-----
  cd /opt/quantopsai
  venv/bin/python3 scripts/backfill_orphan_trailing_stops_2026_06_04.py --dry-run
  venv/bin/python3 scripts/backfill_orphan_trailing_stops_2026_06_04.py --apply
  venv/bin/python3 scripts/backfill_orphan_trailing_stops_2026_06_04.py \
      --profile 15 --apply
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from contextlib import closing
from typing import List, Optional


MASTER_DB = "/opt/quantopsai/quantopsai.db"
PROFILE_DB_TPL = "/opt/quantopsai/quantopsai_profile_{pid}.db"

# Match lines like:
#   backfill_sell: CRM qty=127.0 sell_order=df787e44 @ $172.84 (trailing_stop)
_BACKFILL_RE = re.compile(
    r"backfill_sell:\s+(\w+)\s+qty=([\d.]+)\s+sell_order=(\S+)\s+@\s+"
    r"\$([\d.]+)\s+\((\w+)\)"
)


def _parse_orphans(detail: str) -> List[dict]:
    """Extract orphan-fill records from an audit_alerts.detail blob."""
    out = []
    for line in detail.splitlines():
        m = _BACKFILL_RE.search(line)
        if not m:
            continue
        out.append({
            "symbol": m.group(1),
            "qty": float(m.group(2)),
            "sell_order": m.group(3),
            "sell_price": float(m.group(4)),
            "order_type": m.group(5),
        })
    return out


def _halted_profiles() -> List[dict]:
    """Read currently-halted profiles + their alert detail."""
    with closing(sqlite3.connect(MASTER_DB)) as conn:
        rows = conn.execute(
            "SELECT id, name, halt_reason FROM trading_profiles "
            "WHERE trading_halted = 1 ORDER BY id",
        ).fetchall()
    profiles = []
    for pid, name, reason in rows:
        if not reason or "Reconciler safety net" not in reason:
            continue
        db_path = PROFILE_DB_TPL.format(pid=pid)
        if not os.path.exists(db_path):
            print(f"WARN: pid{pid} db not found at {db_path}; skipping",
                  file=sys.stderr)
            continue
        try:
            with closing(sqlite3.connect(db_path)) as pconn:
                row = pconn.execute(
                    "SELECT detail FROM audit_alerts "
                    "WHERE alert_type='reconciler_synthesis_halt' "
                    "AND resolved=0 ORDER BY created_at DESC LIMIT 1",
                ).fetchone()
        except sqlite3.Error as exc:
            print(f"WARN: pid{pid} audit_alerts read failed: {exc}",
                  file=sys.stderr)
            continue
        if not row or not row[0]:
            continue
        orphans = _parse_orphans(row[0])
        profiles.append({
            "pid": pid, "name": name, "db_path": db_path,
            "halt_reason": reason, "orphans": orphans,
        })
    return profiles


def _resolve_orphan(conn: sqlite3.Connection, orphan: dict) -> dict:
    """Look up the journal rows for one orphan. Returns a plan dict
    describing what we'd write, or {'skip_reason': ...} if we can't /
    shouldn't act."""
    sym, qty, fill_price = orphan["symbol"], orphan["qty"], orphan["sell_price"]
    # Pending_protective row keyed by symbol + qty. Use the NEWEST one
    # — older siblings are leftover from prior replace cycles.
    pending = conn.execute(
        "SELECT id, qty, order_id FROM trades "
        "WHERE symbol=? AND status='pending_protective' AND qty=? "
        "ORDER BY id DESC LIMIT 1",
        (sym, qty),
    ).fetchone()
    if not pending:
        return {"skip_reason": (
            f"no pending_protective row for {sym} qty={qty} — likely "
            f"already cleared by a prior backfill run or by the "
            f"reconciler's chain-walk auto-clear"
        )}
    pending_id, _, pending_oid = pending
    # Open entry BUY (longs only in this orphan class — trailing stops
    # only fire on longs in the current setup).
    entry = conn.execute(
        "SELECT id, qty, price FROM trades "
        "WHERE symbol=? AND side='buy' AND status='open' AND qty=? "
        "AND (occ_symbol IS NULL OR occ_symbol='') "
        "ORDER BY id DESC LIMIT 1",
        (sym, qty),
    ).fetchone()
    if not entry:
        return {"skip_reason": (
            f"no open BUY entry for {sym} qty={qty} — entry already "
            f"closed or qty mismatch (refusing to mutate unfamiliar data)"
        )}
    entry_id, _, entry_price = entry
    entry_price = float(entry_price or 0)
    realized_pnl = round((fill_price - entry_price) * qty, 2)
    # Sibling stale pending_protective rows for the same symbol+qty
    # (other entries in the replace chain that were never closed).
    siblings = conn.execute(
        "SELECT id, order_id FROM trades "
        "WHERE symbol=? AND status='pending_protective' AND qty=? "
        "AND id != ?",
        (sym, qty, pending_id),
    ).fetchall()
    return {
        "symbol": sym,
        "qty": qty,
        "pending_id": pending_id,
        "pending_oid": pending_oid,
        "entry_id": entry_id,
        "entry_price": entry_price,
        "fill_price": fill_price,
        "realized_pnl": realized_pnl,
        "stale_siblings": [
            {"id": s[0], "order_id": s[1]} for s in siblings
        ],
    }


def _apply_plan(conn: sqlite3.Connection, plan: dict,
                 terminal_order_id: str) -> None:
    """Write the plan: close pending row, close entry, cancel stale
    siblings. Mirrors the reconciler's pending_protective UPDATE path."""
    reason_pending = (
        f"backfill 2026-06-04: trailing-stop fill arrived under "
        f"post-replacement order_id {terminal_order_id[:8]}; manually "
        f"matched to placement id {plan['pending_oid'][:8]}"
    )
    conn.execute(
        "UPDATE trades SET status='closed', price=?, fill_price=?, "
        "reason=COALESCE(reason || ' | ', '') || ? WHERE id=?",
        (plan["fill_price"], plan["fill_price"],
         reason_pending, plan["pending_id"]),
    )
    conn.execute(
        "UPDATE trades SET status='closed', pnl=? WHERE id=?",
        (plan["realized_pnl"], plan["entry_id"]),
    )
    for sib in plan["stale_siblings"]:
        conn.execute(
            "UPDATE trades SET status='canceled', "
            "reason=COALESCE(reason || ' | ', '') || ? WHERE id=?",
            (f"backfill 2026-06-04: stale sibling pending_protective "
             f"from prior replace cycle (order_id {sib['order_id'][:8]} "
             f"superseded by {plan['pending_oid'][:8]})",
             sib["id"]),
        )


def _process_profile(profile: dict, apply: bool) -> dict:
    pid = profile["pid"]
    db_path = profile["db_path"]
    stats = {"resolved": 0, "skipped": 0, "stale_canceled": 0}
    print(f"\n=== pid{pid} ({profile['name']}) ===")
    if not profile["orphans"]:
        print("  (no orphans parsed from alert)")
        return stats
    plans = []
    with closing(sqlite3.connect(db_path)) as conn:
        for o in profile["orphans"]:
            plan = _resolve_orphan(conn, o)
            if "skip_reason" in plan:
                print(f"  SKIP {o['symbol']} qty={o['qty']}: "
                      f"{plan['skip_reason']}")
                stats["skipped"] += 1
                continue
            print(f"  PLAN {plan['symbol']} qty={plan['qty']}: "
                  f"pending #{plan['pending_id']} -> closed @ "
                  f"${plan['fill_price']:.2f} | entry #{plan['entry_id']} "
                  f"-> closed (pnl=${plan['realized_pnl']:+.2f}) | "
                  f"{len(plan['stale_siblings'])} stale sibling(s)")
            plans.append((o["sell_order"], plan))
        if apply and plans:
            for terminal_oid, plan in plans:
                _apply_plan(conn, plan, terminal_oid)
                stats["resolved"] += 1
                stats["stale_canceled"] += len(plan["stale_siblings"])
            conn.commit()
            print(f"  APPLIED: {stats['resolved']} orphan(s) resolved, "
                  f"{stats['stale_canceled']} stale sibling(s) canceled")
        elif plans:
            print(f"  --dry-run: would resolve {len(plans)} orphan(s)")
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Write changes (default: dry-run).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Default behavior — show plans without writing.")
    ap.add_argument("--profile", type=int, default=None,
                    help="Only process this single profile id.")
    ap.add_argument("--master-db", default=MASTER_DB)
    args = ap.parse_args(argv)
    if args.apply and args.dry_run:
        print("FATAL: cannot pass both --apply and --dry-run",
              file=sys.stderr)
        return 2
    apply = bool(args.apply)
    profiles = _halted_profiles()
    if args.profile is not None:
        profiles = [p for p in profiles if p["pid"] == args.profile]
        if not profiles:
            print(f"No reconciler-halted profile with id={args.profile}")
            return 0
    if not profiles:
        print("No profiles currently halted by the reconciler safety net.")
        return 0
    print(f"Mode: {'APPLY' if apply else 'DRY-RUN'}")
    print(f"Targets: {[p['pid'] for p in profiles]}")
    totals = {"resolved": 0, "skipped": 0, "stale_canceled": 0}
    for prof in profiles:
        st = _process_profile(prof, apply)
        for k, v in st.items():
            totals[k] += v
    print("\n=== Totals ===")
    print(f"  resolved orphans:       {totals['resolved']}")
    print(f"  skipped (idempotent):   {totals['skipped']}")
    print(f"  stale siblings canceled:{totals['stale_canceled']}")
    if apply:
        print("\nHalts will auto-clear on the next reconciler pass "
              "(see halt_helpers.clear_halt in reconcile_journal_to_broker).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
