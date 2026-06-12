"""Repair the PPCB cross-profile oversells (2026-06-11).

Same race as the BATL oversell (see repair_batl_oversell_2026_06_11
and CHANGELOG): exits fired after the profile's own position was
already closed by a protective fill, selling sibling shares. All
three events predate the prevention deploy (19:32:55Z); broker
order history is clean 1:1 afterwards.

Events (broker order history):
  A2  p96 #182: re-sold 814 @ 3.145885 (order 7a8f2aa0, 19:21:15)
      p97 #235: re-sold   1 @ 2.91     (order 46986c9f, 19:29:02)
      → both consumed p94's 2,712-share lot (19:20:09);
        broker 1,897 vs p94's claim 2,712.
  A3  p102 #187: re-sold 6,668 @ 2.88  (order 952237aa, 19:31:34)
      → consumed p101's 14,142-share lot (19:31:24);
        broker 7,474 vs p101's claim 14,142.

Accounting reattribution, no new orders: void the oversell rows in
the overselling profiles (data_quality tag, cash/FIFO exclude
them); insert closed SELL rows in the deprived profiles carrying
the real broker order ids (their books then equal broker);
re-true realized P&L everywhere touched. Fill timestamps are all
naturally AFTER the deprived profiles' buys, so FIFO needs no
accounting-placement adjustment. Idempotent; refuses unexpected
shapes.

Run:
    venv/bin/python repair_ppcb_oversells_2026_06_11.py            # dry-run
    venv/bin/python repair_ppcb_oversells_2026_06_11.py --apply
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing

REPO_ROOT = "/opt/quantopsai"

# (overseller_pid, row_id, qty, order_prefix)
VOIDS = [
    (96, 182, 814.0, "7a8f2aa0"),
    (97, 235, 1.0, "46986c9f"),
    (102, 187, 6668.0, "952237aa"),
]
# (deprived_pid, qty, fill_price, order_prefix, fill_ts, entry_qty)
REATTRIBUTIONS = [
    (94, 814.0, 3.145885, "7a8f2aa0", "2026-06-11T19:21:15", 2712),
    (94, 1.0, 2.91, "46986c9f", "2026-06-11T19:29:02", 2712),
    (101, 6668.0, 2.88, "952237aa", "2026-06-11T19:31:34", 14142),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    touched = set()

    for pid, row_id, qty, oprefix in VOIDS:
        db = f"{REPO_ROOT}/quantopsai_profile_{pid}.db"
        with closing(sqlite3.connect(db)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT id, qty, status, order_id FROM trades "
                "WHERE id=? AND symbol='PPCB' AND side='sell'",
                (row_id,),
            ).fetchone()
            if row is None or not str(row["order_id"]).startswith(oprefix):
                print(f"p{pid} #{row_id}: shape mismatch — REFUSING")
                return 1
            if row["status"] == "canceled":
                print(f"p{pid} #{row_id}: already voided — skip")
                continue
            if abs(float(row["qty"]) - qty) > 0.5:
                print(f"p{pid} #{row_id}: qty {row['qty']} != {qty} "
                      "— REFUSING")
                return 1
            print(f"p{pid}: {'VOID' if args.apply else 'would void'} "
                  f"sell #{row_id} ({qty:.0f} PPCB) — sibling shares")
            if args.apply:
                conn.execute(
                    "UPDATE trades SET status='canceled', pnl=NULL, "
                    "data_quality='oversold_sibling_shares', "
                    "reason=COALESCE(reason || ' | ', '') || ? "
                    "WHERE id=?",
                    ("repair_ppcb_oversells_2026_06_11: executed "
                     "against a sibling profile's shares — own "
                     "position already closed by protective fill",
                     row_id),
                )
                conn.commit()
                touched.add(pid)

    for pid, qty, price, oprefix, ts, entry_qty in REATTRIBUTIONS:
        db = f"{REPO_ROOT}/quantopsai_profile_{pid}.db"
        with closing(sqlite3.connect(db)) as conn:
            conn.row_factory = sqlite3.Row
            existing = conn.execute(
                "SELECT id FROM trades WHERE order_id LIKE ? "
                "AND side='sell' AND symbol='PPCB'",
                (oprefix + "%",),
            ).fetchone()
            if existing:
                print(f"p{pid}: reattributed sell {oprefix} already "
                      f"present (#{existing['id']}) — skip")
                continue
            entry = conn.execute(
                "SELECT id, qty FROM trades WHERE symbol='PPCB' "
                "AND side='buy' AND status='open'",
            ).fetchone()
            if entry is None or abs(float(entry["qty"]) - entry_qty) > 0.5:
                print(f"p{pid}: expected open {entry_qty}-share PPCB "
                      "entry not found — REFUSING")
                return 1
            print(f"p{pid}: {'INSERT' if args.apply else 'would insert'} "
                  f"closed SELL {qty:.0f} @ {price} ({oprefix})")
            if args.apply:
                conn.execute(
                    "INSERT INTO trades (timestamp, symbol, side, "
                    " qty, price, fill_price, order_id, signal_type, "
                    " strategy, status, reason) "
                    "VALUES (?, 'PPCB', 'sell', ?, ?, ?, ?, 'SELL', "
                    " 'reconcile_reattribution', 'closed', ?)",
                    (ts, qty, price, price,
                     f"{oprefix}-reattributed",
                     "repair_ppcb_oversells_2026_06_11: sibling "
                     "oversold into this profile's lot; this row "
                     "records the broker fill that consumed it"),
                )
                conn.commit()
                touched.add(pid)

    if args.apply and touched:
        sys.path.insert(0, REPO_ROOT)
        from journal import recompute_realized_pnl
        for pid in sorted(touched):
            n = recompute_realized_pnl(
                f"{REPO_ROOT}/quantopsai_profile_{pid}.db")
            print(f"p{pid}: {n} pnl value(s) re-trued")
    print(f"{'APPLIED' if args.apply else 'DRY-RUN'} — done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
