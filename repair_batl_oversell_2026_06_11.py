"""Repair the BATL cross-profile oversell (2026-06-11).

What happened (broker order history, A2 account)
------------------------------------------------
17:42:30  p97 bought 5,145 BATL (order 56d11348)
17:52:59  p97's protective stop FILLED 5,145 (order 515d8f9d)
17:55:23  p97's next exit fired before the stop's fill confirmation
          reached its journal; the old cancel_for_symbol returned
          void on "cancel failed (already filled)" and the SELL
          proceeded: order 8819c37a sold ANOTHER 5,145 — out of
          p94's freshly bought 11,274 (order adf9a14a, fully
          filled, journal row p94 #179).

Result: p94's journal correctly claims 11,274 BATL, but the broker
only holds 6,129 for the account. p97's journal counts sell
proceeds for shares it never owned (#184 — its ~$7.6K equity
overstatement).

What this does (accounting reattribution — no new orders)
---------------------------------------------------------
* p97 row #184 (sell 5,145 @ 1.5097, order 8819c37a): status →
  'canceled' + data_quality tag + pnl cleared. Cash math and FIFO
  then exclude it; p97's books no longer credit the sibling-share
  proceeds.
* p94: INSERT a closed SELL row for 5,145 @ 1.5097 carrying the
  real broker order id 8819c37a — the fill consumed p94's lot, so
  p94's book becomes 6,129 == broker. Tagged so the trades page
  shows it as a reconciler reattribution, not an AI decision.
* Realized P&L re-trued on both profiles afterwards.

Prevention shipped alongside: cancel_for_symbol now reports
already-filled protectives and BOTH exit paths abort instead of
double-selling.

Run:
    venv/bin/python repair_batl_oversell_2026_06_11.py            # dry-run
    venv/bin/python repair_batl_oversell_2026_06_11.py --apply
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing

REPO_ROOT = "/opt/quantopsai"
P97_DB = f"{REPO_ROOT}/quantopsai_profile_97.db"
P94_DB = f"{REPO_ROOT}/quantopsai_profile_94.db"
OVERSELL_ORDER = "8819c37a"  # broker order id prefix of the oversell
QTY = 5145.0
PRICE = 1.5097


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    # --- p97: void the oversell row -------------------------------
    with closing(sqlite3.connect(P97_DB)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, qty, price, status, order_id FROM trades "
            "WHERE symbol='BATL' AND side='sell' AND order_id LIKE ? "
            "AND status='closed'",
            (OVERSELL_ORDER + "%",),
        ).fetchone()
        if row is None:
            print("p97: oversell row not found in expected shape — "
                  "already repaired or shape changed. REFUSING p97 leg.")
        else:
            if abs(float(row["qty"]) - QTY) > 0.5:
                print(f"p97: row #{row['id']} qty {row['qty']} != "
                      f"{QTY} — REFUSING (verify manually)")
                return 1
            print(f"p97: {'VOID' if args.apply else 'would void'} "
                  f"sell row #{row['id']} (5,145 @ {row['price']}) — "
                  "shares belonged to p94")
            if args.apply:
                conn.execute(
                    "UPDATE trades SET status='canceled', pnl=NULL, "
                    "data_quality='oversold_sibling_shares', "
                    "reason=COALESCE(reason || ' | ', '') || ? "
                    "WHERE id=?",
                    ("repair_batl_oversell_2026_06_11: this sell "
                     "executed against p94's shares — p97's own "
                     "position was already closed by its protective "
                     "stop 515d8f9d 2.4 minutes earlier",
                     row["id"]),
                )
                conn.commit()

    # --- p94: record the fill that consumed its lot ----------------
    with closing(sqlite3.connect(P94_DB)) as conn:
        conn.row_factory = sqlite3.Row
        existing = conn.execute(
            "SELECT id FROM trades WHERE order_id LIKE ? "
            "AND side='sell'",
            (OVERSELL_ORDER + "%",),
        ).fetchone()
        if existing:
            print(f"p94: reattributed sell already present "
                  f"(#{existing['id']}) — skipping insert")
        else:
            entry = conn.execute(
                "SELECT id, qty FROM trades WHERE symbol='BATL' "
                "AND side='buy' AND status='open'",
            ).fetchone()
            if entry is None or abs(float(entry["qty"]) - 11274) > 0.5:
                print("p94: expected open 11,274-share BATL entry "
                      "not found — REFUSING p94 leg (verify manually)")
                return 1
            print(f"p94: {'INSERT' if args.apply else 'would insert'} "
                  f"closed SELL 5,145 @ {PRICE} (order "
                  f"{OVERSELL_ORDER}) consuming entry #{entry['id']} "
                  f"partially — book becomes 6,129 == broker")
            if args.apply:
                conn.execute(
                    "INSERT INTO trades (timestamp, symbol, side, "
                    " qty, price, fill_price, order_id, signal_type, "
                    " strategy, status, reason) "
                    "VALUES ('2026-06-11T17:55:23', 'BATL', 'sell', "
                    " ?, ?, ?, ?, 'SELL', 'reconcile_reattribution', "
                    " 'closed', ?)",
                    (QTY, PRICE, PRICE,
                     "8819c37a-5dc8-46e8-9f5c-reattributed",
                     "repair_batl_oversell_2026_06_11: sibling p97 "
                     "oversold into this profile's lot; this row "
                     "records the broker fill that consumed 5,145 "
                     "of the 11,274-share entry"),
                )
                conn.commit()

    if args.apply:
        sys.path.insert(0, REPO_ROOT)
        from journal import recompute_realized_pnl
        for db in (P97_DB, P94_DB):
            n = recompute_realized_pnl(db)
            print(f"{db.rsplit('/', 1)[-1]}: {n} pnl value(s) re-trued")
    print(f"{'APPLIED' if args.apply else 'DRY-RUN'} — done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
