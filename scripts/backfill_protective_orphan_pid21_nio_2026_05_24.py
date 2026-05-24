"""One-time backfill: pid21 NIO trailing-stop orphan from 2026-05-22.

Background
----------
Before the 2026-05-21 fix to `bracket_orders.submit_protective_*`,
protective orders were placed via `api.submit_order` but no journal row
was written at placement time — only the `protective_*_order_id` was
stamped onto the entry trade's column. When the broker autonomously
filled the protective order, the reconciler saw a fill with no matching
trades row, treated it as an "orphan," and tripped the safety-net halt
every cycle.

pid21 (EXP-A3-25K-Candidate)'s NIO trailing stop fired on 2026-05-22 at
$5.18 (84 shares) but the SELL row was never written. The entry BUY
(trade #23) stayed `status='open'` and the reconciler halted the
profile repeatedly (halt set 2026-05-22T21:45 UTC).

This is the identical class of orphan fixed by
`backfill_protective_orphan_pid24_qcom_2026_05_21.py`; all NEW
protective placements now write a `pending_protective` row at placement
time, so this can't recur. This script cleans up the one legacy NIO
case so the halt can clear.

What this script does
---------------------
1. Verifies the expected state on pid21's journal (trade #23 is the
   open NIO BUY 84 with a non-empty protective_trailing_order_id; no
   row exists yet for that order_id).
2. INSERTs the missing SELL row with the broker's fill data
   (side='sell', qty=84, price=5.18, order_id=<entry's trailing id>,
   signal_type='reconcile_backfill', status='closed').
3. UPDATEs the entry BUY (trade #23) to status='closed' with realized
   pnl = (5.18 - entry_price) * 84.
4. Verifies post-state.

Idempotency
-----------
Skips with a clear message if the SELL row already exists, the entry
BUY is already closed, or the entry's symbol/qty doesn't match the
expected legacy state (refuses to act on unfamiliar data).

Usage
-----
On prod:
  cd /opt/quantopsai
  venv/bin/python3 scripts/backfill_protective_orphan_pid21_nio_2026_05_24.py --dry-run
  venv/bin/python3 scripts/backfill_protective_orphan_pid21_nio_2026_05_24.py
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from contextlib import closing


PROFILE_DB = "/opt/quantopsai/quantopsai_profile_21.db"

# Expected legacy state (from the reconcile dry-run / halt alert detail)
ENTRY_TRADE_ID = 23
EXPECTED_SYMBOL = "NIO"
EXPECTED_ENTRY_QTY = 84.0

# Broker fill data (from the reconcile dry-run detail line)
FILL_PRICE = 5.18
FILL_QTY = 84.0
FILL_TIMESTAMP = "2026-05-22T21:45:00"  # approximate; exact filled_at not captured
FILL_ORDER_TYPE = "trailing_stop"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would happen without writing.")
    ap.add_argument("--db", default=PROFILE_DB,
                    help="Path to pid21's journal DB.")
    args = ap.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"FATAL: DB not found: {args.db}", file=sys.stderr)
        return 2

    with closing(sqlite3.connect(args.db)) as conn:
        conn.row_factory = sqlite3.Row

        entry = conn.execute(
            "SELECT id, symbol, side, qty, price, status, "
            "       protective_trailing_order_id "
            "FROM trades WHERE id = ?",
            (ENTRY_TRADE_ID,),
        ).fetchone()
        if entry is None:
            print(f"FATAL: trade {ENTRY_TRADE_ID} not found", file=sys.stderr)
            return 2
        if entry["symbol"] != EXPECTED_SYMBOL:
            print(f"FATAL: expected symbol={EXPECTED_SYMBOL}, got "
                  f"{entry['symbol']} — refusing to act", file=sys.stderr)
            return 2
        if entry["status"] == "closed":
            print(f"SKIP: entry trade #{ENTRY_TRADE_ID} is already "
                  f"status=closed — backfill likely already ran")
            return 0
        if abs(float(entry["qty"]) - EXPECTED_ENTRY_QTY) > 0.01:
            print(f"FATAL: expected entry qty={EXPECTED_ENTRY_QTY}, "
                  f"got {entry['qty']} — refusing to act on unfamiliar "
                  f"state", file=sys.stderr)
            return 2

        trailing_oid = entry["protective_trailing_order_id"]
        if not trailing_oid:
            print(f"FATAL: trade #{ENTRY_TRADE_ID} has no "
                  f"protective_trailing_order_id — this is not the "
                  f"expected trailing-stop orphan; refusing to act",
                  file=sys.stderr)
            return 2

        existing = conn.execute(
            "SELECT id, status FROM trades WHERE order_id = ?",
            (trailing_oid,),
        ).fetchone()
        if existing:
            print(f"SKIP: row with order_id={trailing_oid} already "
                  f"exists (id={existing['id']}, status={existing['status']})")
            return 0

        realized_pnl = round((FILL_PRICE - float(entry["price"])) * FILL_QTY, 2)
        reason_text = (
            f"one-time backfill 2026-05-24: legacy protective "
            f"trailing-stop fill from before pre-journaling code "
            f"shipped (entry trade={ENTRY_TRADE_ID})"
        )

        print()
        print("=== Plan ===")
        print(f"  entry trade #{ENTRY_TRADE_ID}: {EXPECTED_SYMBOL} "
              f"qty={entry['qty']} @ ${float(entry['price']):.2f} "
              f"(status={entry['status']})")
        print(f"  INSERT SELL row: qty={FILL_QTY} @ ${FILL_PRICE:.2f}, "
              f"order_id={trailing_oid}, status=closed")
        print(f"  UPDATE entry #{ENTRY_TRADE_ID}: status=closed, "
              f"pnl=${realized_pnl:+.2f}")
        print()

        if args.dry_run:
            print("--dry-run: no writes performed")
            return 0

        conn.execute(
            "INSERT INTO trades "
            "(timestamp, symbol, side, qty, price, fill_price, "
            " order_id, signal_type, status, reason) "
            "VALUES (?, ?, 'sell', ?, ?, ?, ?, "
            "        'reconcile_backfill', 'closed', ?)",
            (FILL_TIMESTAMP, EXPECTED_SYMBOL, FILL_QTY,
             FILL_PRICE, FILL_PRICE, trailing_oid, reason_text),
        )
        sell_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "UPDATE trades SET status='closed', pnl=? WHERE id=?",
            (realized_pnl, ENTRY_TRADE_ID),
        )
        conn.commit()
        print(f"OK: inserted SELL row id={sell_id}, marked entry trade "
              f"#{ENTRY_TRADE_ID} closed with pnl=${realized_pnl:+.2f}")
        print()
        print("Next reconcile cycle should auto-clear the profile's halt.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
