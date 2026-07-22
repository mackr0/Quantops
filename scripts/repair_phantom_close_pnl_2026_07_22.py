#!/usr/bin/env python3
"""Repair 2026-07-22 — void pending_fill rows whose orders the broker
does not recognize (or that are terminal-unfilled), clearing phantom
booked P&L.

The incident: p212's GOOG 375/380 close pair (trade ids 257/258) sat
status='pending_fill' for a week with $2,460 + $2,148 = $4,608 of
booked pnl. The broker never filled — the submits died in the 429
storm — and get_order on those ids raises "order not found", which the
fill-healer treated as transient and skipped forever. check_
decomposition counts pending_fill pnl as realized, so the $4,608 held
the integrity gap open and the kill switch stayed latched.

What it does, per profile DB:
  1. SELECT rows with status='pending_fill', a non-NULL order_id, and
     timestamp older than 24h.
  2. For each, ask the broker for the order:
       - "order not found" / 404            -> VOID candidate
       - terminal status with filled_qty==0 -> VOID candidate
       - anything else (live, filled, error other than 404) -> SKIP
         and report; nothing is guessed.
  3. DRY-RUN by default: prints the plan. With --apply, sets
     status='canceled' (decomposition and the virtual books both
     exclude canceled rows; pnl is left in place for audit — it no
     longer counts).

Run on the droplet from /opt/quantopsai:
    python3 scripts/repair_phantom_close_pnl_2026_07_22.py            # dry run
    python3 scripts/repair_phantom_close_pnl_2026_07_22.py --apply    # execute

The permanent fix (same commit) lives in _task_update_fills: a >24h
"order not found" row is now voided automatically, so this class heals
itself in future and this script exists only to clear today's backlog.
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

TERMINAL = {"filled", "canceled", "expired", "rejected", "done_for_day"}


def _order_verdict(api, order_id):
    """Returns ('void', reason) / ('skip', reason)."""
    try:
        order = api.get_order(order_id)
    except Exception as exc:
        s = str(exc).lower()
        if "not found" in s or "404" in s:
            return ("void", "order UNKNOWN at broker (404)")
        return ("skip", f"broker lookup failed ({type(exc).__name__}: {exc})")
    if order is None:
        return ("void", "broker returned None for order id")
    status = (getattr(order, "status", "") or "").lower()
    try:
        filled_qty = float(getattr(order, "filled_qty", 0) or 0)
    except (TypeError, ValueError):
        filled_qty = 0.0
    if status in TERMINAL and status != "filled" and filled_qty == 0:
        return ("void", f"terminal-unfilled at broker (status={status})")
    return ("skip", f"order live or filled (status={status}, "
                    f"filled_qty={filled_qty}) — NOT voiding")


def _repair_one_db(conn, pid, apply_changes, cutoff, totals):
    conn.row_factory = sqlite3.Row
    # Some profile DBs are empty shells (created but never initialized
    # or never traded) — probe for the trades table so one of them
    # can't abort the whole repair run.
    has_trades = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='trades'").fetchone()
    if not has_trades:
        return
    rows = conn.execute(
        "SELECT id, timestamp, symbol, side, qty, pnl, status, "
        "       order_id, occ_symbol FROM trades "
        "WHERE status = 'pending_fill' AND order_id IS NOT NULL "
        "  AND replace(substr(timestamp, 1, 19), 'T', ' ') < ?",
        (cutoff,),
    ).fetchall()
    if not rows:
        return
    from models import build_user_context_from_profile
    from client import get_api
    try:
        api = get_api(build_user_context_from_profile(pid))
    except Exception as exc:
        print(f"p{pid}: cannot build broker api ({exc}) — SKIPPING "
              f"{len(rows)} candidate row(s), nothing changed")
        return
    for r in rows:
        verdict, reason = _order_verdict(api, r["order_id"])
        label = (f"p{pid} trade #{r['id']} {r['side']} "
                 f"{r['occ_symbol'] or r['symbol']} qty={r['qty']} "
                 f"pnl={r['pnl']} order={str(r['order_id'])[:8]}")
        if verdict == "void":
            totals["void"] += 1
            if apply_changes:
                conn.execute(
                    "UPDATE trades SET status = 'canceled', price = 0 "
                    "WHERE id = ?", (r["id"],))
                conn.commit()
                print(f"VOIDED  {label} — {reason}")
            else:
                print(f"WOULD VOID  {label} — {reason}")
        else:
            totals["skip"] += 1
            print(f"SKIP    {label} — {reason}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="execute the voids (default: dry run)")
    ap.add_argument("--db-glob", default="/opt/quantopsai/quantopsai_profile_*.db")
    args = ap.parse_args()

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)
              ).strftime("%Y-%m-%d %H:%M:%S")
    totals = {"void": 0, "skip": 0}

    for db_path in sorted(glob.glob(args.db_glob)):
        m = re.search(r"profile_(\d+)\.db$", db_path)
        if not m:
            continue
        conn = sqlite3.connect(db_path)
        try:
            _repair_one_db(conn, int(m.group(1)), args.apply,
                           cutoff, totals)
        finally:
            conn.close()

    mode = "APPLIED" if args.apply else "DRY RUN (use --apply to execute)"
    print(f"\n{mode}: {totals['void']} void(s), {totals['skip']} skip(s).")
    if totals["void"] and not args.apply:
        print("Review the plan above, then re-run with --apply.")
    if args.apply and totals["void"]:
        print("The integrity gate re-checks every cycle; the kill switch "
              "auto-clears once the decomposition gap closes.")


if __name__ == "__main__":
    main()
