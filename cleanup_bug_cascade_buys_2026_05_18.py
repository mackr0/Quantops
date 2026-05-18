"""ONE-OFF: sell down the bug-cascade BUY excess across A1 profiles.

On 2026-05-18 the reconcile_trade_statuses bug (commit ca2cdac fixed
it) repeatedly closed every open BUY when the broker returned an
empty positions list. The buy-side strategies (buy_hold, random) then
rebalanced each cycle because virtual_positions reported "0 SPY" /
"0 of any random pick," so they re-bought. Net effect: roughly 2×
the intended position per profile.

This script restores each profile to its INTENDED day-1 position by
computing per-symbol excess (net_held − first_buy_qty) and submitting
a SELL for that excess through the exact same primitives the trade
pipeline uses:
  - ctx.get_alpaca_api().submit_order(...)
  - journal.log_trade(...)

It does NOT go through trader.execute_trade()'s constraint-checking
wrapper because that path is signal-driven and our submit is a
manual cleanup, not a strategy decision. But it uses the same low-
level Alpaca call + journal log as that wrapper — so any pipeline
bug downstream of submit will surface here too (which is the point
per the operator's "expose bugs, don't hide them" guidance).

Each cleanup row is journaled with:
  signal_type = 'manual_cleanup_2026_05_18'
  strategy    = 'bug_cascade_cleanup'
  reason      = 'sell excess from journal.py:1554 cascading rebalance'

Run:
    cd /opt/quantopsai && set -a; . ./.env; set +a; \
        /opt/quantopsai/venv/bin/python cleanup_bug_cascade_buys_2026_05_18.py
    # Add --apply to actually submit.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from contextlib import closing


CUTOFF = "2026-05-18T13:30:00"


def compute_excess_per_symbol(db_path: str) -> dict[str, dict]:
    """For each symbol traded today: net_held, first_buy_qty, excess.
    Excess is what we need to SELL to restore the intended day-1
    position. Skip symbols where net_held <= first_buy_qty (no excess
    or already correctly sized)."""
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT id, timestamp, symbol, side, qty FROM trades "
            "WHERE timestamp >= ? ORDER BY timestamp ASC, id ASC",
            (CUTOFF,),
        ).fetchall()
    per_sym: dict[str, dict] = {}
    for _id, _ts, sym, side, qty in rows:
        if qty is None:
            continue
        d = per_sym.setdefault(sym, {"net": 0.0, "first_buy": None})
        if side == "buy":
            if d["first_buy"] is None:
                d["first_buy"] = float(qty)
            d["net"] += float(qty)
        elif side in ("sell", "cover"):
            d["net"] -= float(qty)
    out = {}
    for sym, d in per_sym.items():
        if d["first_buy"] is None:
            continue
        excess = d["net"] - d["first_buy"]
        if excess > 0:
            out[sym] = {
                "net_held": d["net"],
                "intended": d["first_buy"],
                "excess": excess,
            }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Actually submit SELL orders. Default is dry-run.")
    ap.add_argument("--profile-ids", default="12,13,14",
                    help="Comma-separated profile IDs (default: A1 trio)")
    args = ap.parse_args()

    sys.path.insert(0, "/opt/quantopsai")
    from models import build_user_context_from_profile
    from journal import log_trade

    pids = [int(x) for x in args.profile_ids.split(",")]
    total_orders = 0
    total_shares = 0

    for pid in pids:
        db = f"/opt/quantopsai/quantopsai_profile_{pid}.db"
        if not os.path.exists(db):
            print(f"P{pid}: DB missing — skipping")
            continue
        excess = compute_excess_per_symbol(db)
        if not excess:
            print(f"P{pid}: no excess to clean")
            continue

        print(f"\n--- P{pid} ---")
        try:
            ctx = build_user_context_from_profile(pid)
            api = ctx.get_alpaca_api()
        except Exception as e:
            print(f"  ctx build failed: {type(e).__name__}: {e}")
            continue

        for sym, d in sorted(excess.items()):
            qty = int(d["excess"])
            if qty <= 0:
                continue
            print(f"  {sym:6}  held={d['net_held']:>6.0f}  "
                  f"intended={d['intended']:>6.0f}  "
                  f"SELL {qty:>5} shares (excess)")
            if not args.apply:
                continue
            # Use close_position(symbol, qty=N) instead of submit_order.
            # The bare submit_order path failed for most symbols on the
            # first apply attempt with "insufficient qty available" —
            # protective stops from the original BUYs had encumbered
            # the full position, so available=0 even though held=N.
            # close_position atomically cancels conflicting open orders
            # and submits the partial-close. The stop_coverage scheduled
            # task re-applies protective stops on the remaining intended
            # position within minutes.
            try:
                order = api.close_position(sym, qty=str(qty))
                log_trade(
                    symbol=sym,
                    side="sell",
                    qty=qty,
                    price=0.0,  # market order — fill price written later
                    order_id=order.id,
                    signal_type="manual_cleanup_2026_05_18",
                    strategy="bug_cascade_cleanup",
                    reason=("sell excess from journal.py:1554 "
                            "cascading rebalance"),
                    db_path=db,
                )
                print(f"      submitted: order_id={order.id} "
                      f"status={order.status}")
                total_orders += 1
                total_shares += qty
                time.sleep(0.2)  # gentle pacing
            except Exception as e:
                print(f"      CLOSE FAILED: {type(e).__name__}: {e}")

    print()
    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"{mode}: {total_orders} orders, {total_shares} total shares")
    if not args.apply:
        print("Re-run with --apply to submit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
