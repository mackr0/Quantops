"""2026-07-20 — READ-ONLY diagnostic #2: broker-order truth for the
open journal shorts the broker no longer holds.

Diagnostic #1 showed the drifted positions are REAL side='short'
entries (opened Jul 10-13) whose broker-side protective trailing
stops were re-placed daily and journaled 'canceled' on each
replacement. Hypothesis: the final stops FILLED at the broker
(covering the shorts) but the journal rows for them say 'canceled',
so no cover was ever recorded.

This prints, per account:
  1. every broker order for GOOG/GOOGL/WMT/CVX since 2026-07-15 —
     id, side, type, qty, filled_qty, filled_avg_price, status,
     submitted/filled times, replaces/replaced_by
  2. per profile: each OPEN short entry row for those symbols with
     its protective_*_order_id pointers, so fills can be matched to
     the entry that owns them

Writes NOTHING.
"""
from __future__ import annotations

import collections
import os
import sqlite3
import sys
from contextlib import closing

sys.path.insert(0, "/opt/quantopsai")
sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")))

SYMBOLS = ["GOOG", "GOOGL", "WMT", "CVX"]
SINCE = "2026-07-15T00:00:00Z"


def main():
    from models import get_active_profiles, build_user_context_from_profile

    by_account = collections.defaultdict(list)
    for prof in get_active_profiles():
        by_account[prof.get("alpaca_account_id")].append(prof["id"])

    for aid, pids in sorted(by_account.items(), key=lambda kv: str(kv[0])):
        print(f"\n================ account {aid} ================")
        api = None
        for pid in pids:
            try:
                api = build_user_context_from_profile(pid).get_alpaca_api()
                break
            except Exception as exc:
                print(f"  (profile {pid}: api unavailable: {exc})")
        if api is None:
            print("  NO API ACCESS — skipped")
            continue

        print(f"--- broker orders for {'/'.join(SYMBOLS)} since {SINCE} ---")
        try:
            try:
                orders = api.list_orders(status="all", symbols=SYMBOLS,
                                         after=SINCE, direction="asc",
                                         limit=500)
            except TypeError:
                orders = [o for o in api.list_orders(status="all",
                                                     limit=500,
                                                     direction="desc")
                          if getattr(o, "symbol", "") in SYMBOLS]
        except Exception as exc:
            print(f"  list_orders failed: {exc}")
            orders = []
        for o in orders or []:
            print(
                f"  {getattr(o, 'symbol', '?'):6s} "
                f"{getattr(o, 'side', '?'):4s} "
                f"{getattr(o, 'order_type', getattr(o, 'type', '?')):14s} "
                f"qty={getattr(o, 'qty', '?')} "
                f"filled={getattr(o, 'filled_qty', '?')} "
                f"@{getattr(o, 'filled_avg_price', None)} "
                f"status={getattr(o, 'status', '?'):16s} "
                f"sub={str(getattr(o, 'submitted_at', ''))[:19]} "
                f"filled_at={str(getattr(o, 'filled_at', ''))[:19]} "
                f"id={getattr(o, 'id', '?')} "
                f"replaces={getattr(o, 'replaces', None)} "
                f"replaced_by={getattr(o, 'replaced_by', None)}"
            )

        for pid in pids:
            ctx = build_user_context_from_profile(pid)
            with closing(sqlite3.connect(ctx.db_path)) as conn:
                conn.row_factory = sqlite3.Row
                cols = {r[1] for r in conn.execute(
                    "PRAGMA table_info(trades)").fetchall()}
                ptr_cols = [c for c in (
                    "protective_stop_order_id", "protective_tp_order_id",
                    "protective_trailing_order_id") if c in cols]
                sel = ", ".join(["id", "timestamp", "symbol", "qty",
                                 "order_id"] + ptr_cols)
                rows = conn.execute(
                    f"SELECT {sel} FROM trades "
                    "WHERE side='short' AND occ_symbol IS NULL "
                    "AND COALESCE(status,'open')='open' "
                    "AND symbol IN (?, ?, ?, ?)",
                    SYMBOLS,
                ).fetchall()
            if not rows:
                continue
            print(f"--- profile {pid}: OPEN short entries + pointers ---")
            for r in rows:
                ptrs = " ".join(f"{c}={r[c]}" for c in ptr_cols)
                print(f"  id={r['id']} {r['timestamp'][:19]} "
                      f"{r['symbol']} qty={r['qty']:g} "
                      f"entry_oid={r['order_id']} {ptrs}")
    print("\n(read-only diagnostic — nothing was modified)")


if __name__ == "__main__":
    main()
