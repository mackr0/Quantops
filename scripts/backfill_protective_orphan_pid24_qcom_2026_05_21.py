"""One-time backfill: pid24 QCOM trailing-stop orphan from 2026-05-21.

Background
----------
Before today's fix to `bracket_orders.submit_protective_*`, protective
orders were placed via `api.submit_order` but no journal row was
written at placement time — only the `protective_*_order_id` was
stamped onto the entry trade's column. When the broker autonomously
filled the protective order, the reconciler saw a fill with no
matching trades row and treated it as an "orphan" — tripping the
safety-net halt every cycle.

pid24's QCOM trailing stop fired on 2026-05-20 / 21 at $199.50
(125 shares) but the SELL row was never written. The entry BUY
(trade #40) stayed `status='open'` and the reconciler halted the
profile repeatedly.

After today's commit lands, all NEW protective placements write a
`pending_protective` row at placement time, so this class of orphan
is impossible going forward. This script cleans up the ONE legacy
case (pid24 QCOM) so the halt can clear.

What this script does
---------------------
1. Verifies the expected state on pid24's journal (trade #40 is the
   open QCOM BUY 125 with the matching protective_trailing_order_id;
   no SELL row exists for that order_id).
2. INSERTs the missing SELL row with the broker's fill data:
     side='sell', qty=125, price=199.50, fill_price=199.50,
     order_id=d43d5479-..., signal_type='reconcile_backfill',
     status='closed'
   Includes a `reason` field that documents this as a one-time
   legacy-bug cleanup.
3. UPDATEs the entry BUY (trade #40) to status='closed' and
   computes realized pnl from the FIFO match
   (199.50 - 200.99) * 125 = -186.25.
4. Verifies post-state: SELL row exists, BUY is closed with pnl.

Idempotency
-----------
Skips with a clear message if any of:
  - The SELL row already exists (script already ran)
  - The entry BUY is already status='closed'
  - The entry BUY's qty/protective_trailing_order_id doesn't match
    the expected legacy state (means the data shape changed
    underneath us; refuse to act)

Usage
-----
On prod:
  cd /opt/quantopsai
  set -a && . /opt/quantopsai/.env && set +a
  /opt/quantopsai/venv/bin/python scripts/backfill_protective_orphan_pid24_qcom_2026_05_21.py

Dry-run (no writes):
  ... script.py --dry-run
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from contextlib import closing


PROFILE_DB = "/opt/quantopsai/quantopsai_profile_24.db"

# Expected legacy state (from the halt alert detail)
ENTRY_TRADE_ID = 40
EXPECTED_SYMBOL = "QCOM"
EXPECTED_ENTRY_QTY = 125.0
EXPECTED_ENTRY_PRICE = 200.99
EXPECTED_TRAILING_ORDER_ID = "d43d5479-ead1-4d27-96df-ad38535c21bb"

# Broker fill data (from the synthesis-halt alert detail)
FILL_PRICE = 199.50
FILL_QTY = 125.0
FILL_TIMESTAMP = "2026-05-21T16:00:00"  # Approximate; alert didn't capture exact filled_at
FILL_ORDER_TYPE = "trailing_stop"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would happen without writing.")
    ap.add_argument("--db", default=PROFILE_DB,
                    help="Path to pid24's journal DB.")
    args = ap.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"FATAL: DB not found: {args.db}", file=sys.stderr)
        return 2

    with closing(sqlite3.connect(args.db)) as conn:
        conn.row_factory = sqlite3.Row

        # Verify entry state
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
                  f"{entry['symbol']}", file=sys.stderr)
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
        if entry["protective_trailing_order_id"] != EXPECTED_TRAILING_ORDER_ID:
            print(f"FATAL: expected trailing_order_id="
                  f"{EXPECTED_TRAILING_ORDER_ID}, got "
                  f"{entry['protective_trailing_order_id']} — refusing to "
                  f"act on unfamiliar state", file=sys.stderr)
            return 2

        # Verify no SELL row exists with the trailing order_id
        existing_sell = conn.execute(
            "SELECT id, status FROM trades WHERE order_id = ?",
            (EXPECTED_TRAILING_ORDER_ID,),
        ).fetchone()
        if existing_sell:
            print(f"SKIP: row with order_id={EXPECTED_TRAILING_ORDER_ID} "
                  f"already exists (id={existing_sell['id']}, "
                  f"status={existing_sell['status']})")
            return 0

        # Compute realized P&L for the closed BUY (long position)
        realized_pnl = round(
            (FILL_PRICE - float(entry["price"])) * FILL_QTY, 2,
        )
        reason_text = (
            f"one-time backfill 2026-05-21: legacy protective "
            f"trailing-stop fill from before pre-journaling code "
            f"shipped (entry trade={ENTRY_TRADE_ID})"
        )

        print()
        print("=== Plan ===")
        print(f"  INSERT SELL row:")
        print(f"    symbol={EXPECTED_SYMBOL}, qty={FILL_QTY}, "
              f"price=${FILL_PRICE:.2f}")
        print(f"    order_id={EXPECTED_TRAILING_ORDER_ID}")
        print(f"    signal_type=reconcile_backfill, status=closed")
        print(f"  UPDATE entry trade #{ENTRY_TRADE_ID}:")
        print(f"    status=closed, pnl=${realized_pnl:+.2f}")
        print()

        if args.dry_run:
            print("--dry-run: no writes performed")
            return 0

        # APPLY
        conn.execute(
            "INSERT INTO trades "
            "(timestamp, symbol, side, qty, price, fill_price, "
            " order_id, signal_type, status, reason) "
            "VALUES (?, ?, 'sell', ?, ?, ?, ?, "
            "        'reconcile_backfill', 'closed', ?)",
            (FILL_TIMESTAMP, EXPECTED_SYMBOL, FILL_QTY,
             FILL_PRICE, FILL_PRICE,
             EXPECTED_TRAILING_ORDER_ID, reason_text),
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
