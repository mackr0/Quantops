"""2026-07-20 — READ-ONLY diagnostic #3: WHO authored Monday's
un-journaled short covers?

Diagnostic #2 found market BUY orders at Mon 13:30-13:52 UTC that
cover every drifted short share-for-share, journaled nowhere. Two
possible authors:
  a) a system component that bypassed journaling — then the order id
     appears in some profile's `submitted_orders` door ledger, and
     the Alpaca client_order_id follows the system's format
  b) the broker itself (Alpaca-initiated liquidation) — then no
     ledger entry anywhere and an Alpaca-generated client_order_id

Prints, per account: each Monday cover order's client_order_id +
full detail, and which profile DB (if any) has it in
submitted_orders or anywhere in trades. Writes NOTHING.
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
SINCE = "2026-07-19T00:00:00Z"


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
            except Exception:
                continue
        if api is None:
            print("  NO API ACCESS — skipped")
            continue
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
            continue

        fills = [o for o in orders or []
                 if str(getattr(o, "status", "")) == "filled"]
        for o in fills:
            oid = str(getattr(o, "id", ""))
            coid = getattr(o, "client_order_id", None)
            print(f"\n  {getattr(o, 'symbol', '?'):6s} "
                  f"{getattr(o, 'side', '?'):4s} "
                  f"filled={getattr(o, 'filled_qty', '?')} "
                  f"@{getattr(o, 'filled_avg_price', None)} "
                  f"filled_at={str(getattr(o, 'filled_at', ''))[:19]}")
            print(f"    id               = {oid}")
            print(f"    client_order_id  = {coid}")
            for pid in pids:
                ctx = build_user_context_from_profile(pid)
                with closing(sqlite3.connect(ctx.db_path)) as conn:
                    tabs = {r[0] for r in conn.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='table'").fetchall()}
                    in_ledger = in_trades = False
                    ledger_detail = ""
                    if "submitted_orders" in tabs:
                        row = conn.execute(
                            "SELECT * FROM submitted_orders "
                            "WHERE order_id = ?", (oid,)).fetchone()
                        if row:
                            in_ledger = True
                            ledger_detail = repr(tuple(row))
                    row = conn.execute(
                        "SELECT id, side, signal_type, status FROM trades "
                        "WHERE order_id = ?", (oid,)).fetchone()
                    if row:
                        in_trades = True
                if in_ledger or in_trades:
                    print(f"    profile {pid}: "
                          f"ledger={'YES ' + ledger_detail if in_ledger else 'no'} "
                          f"trades={'YES ' + repr(tuple(row)) if in_trades else 'no'}")
            else:
                pass
    print("\n(read-only diagnostic — nothing was modified)")


if __name__ == "__main__":
    main()
