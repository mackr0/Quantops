"""2026-07-20 — READ-ONLY diagnostic for the opex phantom shorts.

The repair dry-run found ZERO fabricated OPTION_EXERCISE legs, so the
journal shorts come from a different writer than predicted. This
prints, per active profile, everything needed to identify it:

  1. every trades row since 2026-07-10 for the drifted symbols
     (GOOG/GOOGL/WMT/CVX) — stock AND option rows
  2. every broker-activity row (signal_type OPASN/OPEXP/OPXRC) since
     2026-07-01, ANY symbol — including ones written with
     occ_symbol=NULL (a stock-shaped row from an option activity is
     the prime suspect: activities_capture writes side='sell'
     unconditionally, so a stock-symboled activity becomes a stock
     SELL the FIFO books as a short)
  3. the profile's current virtual stock positions for those symbols

Writes NOTHING. Safe to run any time.
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

SYMBOLS = ("GOOG", "GOOGL", "WMT", "CVX")


def main():
    from models import get_active_profiles, build_user_context_from_profile
    from journal import get_virtual_positions

    by_account = collections.defaultdict(list)
    for prof in get_active_profiles():
        by_account[prof.get("alpaca_account_id")].append(prof["id"])

    for aid, pids in sorted(by_account.items(), key=lambda kv: str(kv[0])):
        print(f"\n================ account {aid} ================")
        for pid in pids:
            ctx = build_user_context_from_profile(pid)
            db = ctx.db_path
            with closing(sqlite3.connect(db)) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT id, timestamp, symbol, side, qty, price, "
                    "       fill_price, order_id, signal_type, strategy, "
                    "       status, occ_symbol, expiry, strike, "
                    "       substr(COALESCE(reason,''), 1, 90) AS reason "
                    "FROM trades "
                    "WHERE (symbol IN (?, ?, ?, ?) "
                    "       AND timestamp >= '2026-07-10') "
                    "   OR (signal_type IN ('OPASN','OPEXP','OPXRC') "
                    "       AND timestamp >= '2026-07-01') "
                    "ORDER BY timestamp ASC, id ASC",
                    SYMBOLS,
                ).fetchall()
                virt = {p["symbol"]: p["qty"] for p in get_virtual_positions(
                            db_path=db, price_fetcher=lambda s: 0.0)
                        if not p.get("occ_symbol")
                        and p["symbol"] in SYMBOLS}
            if not rows and not virt:
                continue
            print(f"\n--- profile {pid} ({db}) ---")
            print(f"virtual stock ({'/'.join(SYMBOLS)}): {virt or '{}'}")
            for r in rows:
                print(
                    f"  id={r['id']} {r['timestamp'][:19]} "
                    f"{r['symbol']:6s} {r['side']:5s} "
                    f"qty={r['qty']:g} px={r['price']} "
                    f"fill={r['fill_price']} sig={r['signal_type']} "
                    f"st={r['status']} occ={r['occ_symbol'] or '-'} "
                    f"oid={str(r['order_id'])[:20]} :: {r['reason']}"
                )
    print("\n(read-only diagnostic — nothing was modified)")


if __name__ == "__main__":
    main()
