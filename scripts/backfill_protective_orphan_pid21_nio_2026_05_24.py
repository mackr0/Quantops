"""One-time fix: pid21 NIO trailing-stop reconcile mismatch (2026-05-22).

Root cause
----------
pid21 (EXP-A3-25K-Candidate) placed a NIO trailing stop, which the
broker pre-journaled as a `pending_protective` SELL row (#28) carrying
the original order id `69b9ab76`. As the stop trailed, the broker
REPLACED that order — so the order that actually filled on 2026-05-22
(@ $5.18, 84 sh) has a *different* id. `_detect_protective_fill` only
treats the recorded id as the fill when its broker status == 'filled';
the original `69b9ab76` shows as replaced/canceled, so detection falls
through to the fuzzy fallback, which reports the fill under the
replacement id. That id doesn't match pending row #28, so the reconciler
can't flip #28 to closed and re-halts the profile every pass.

The position IS closed at the broker. This script makes the journal
match reality the same way the reconciler's protective path would have:
flip pending SELL #28 -> closed at the fill price, and close entry
BUY #23 with realized P&L. After that the reconciler sees no open NIO
entry, no orphan, and auto-clears the halt.

Idempotency
-----------
Skips if entry #23 is already closed, or if the rows don't match the
expected NIO/84 shape (refuses to act on unfamiliar data).

Usage
-----
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

ENTRY_TRADE_ID = 23       # open NIO BUY
PENDING_SELL_ID = 28      # pending_protective NIO SELL
EXPECTED_SYMBOL = "NIO"
EXPECTED_QTY = 84.0
FILL_PRICE = 5.18         # broker trailing-stop fill (from reconcile detection)


def _check(row, rid, side, status_expected):
    if row is None:
        print(f"FATAL: trade #{rid} not found", file=sys.stderr)
        return False
    if row["symbol"] != EXPECTED_SYMBOL:
        print(f"FATAL: #{rid} symbol={row['symbol']} (expected "
              f"{EXPECTED_SYMBOL}) — refusing", file=sys.stderr)
        return False
    if (row["side"] or "").lower() != side:
        print(f"FATAL: #{rid} side={row['side']} (expected {side}) — "
              f"refusing", file=sys.stderr)
        return False
    if abs(float(row["qty"]) - EXPECTED_QTY) > 0.01:
        print(f"FATAL: #{rid} qty={row['qty']} (expected {EXPECTED_QTY}) "
              f"— refusing", file=sys.stderr)
        return False
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would happen without writing.")
    ap.add_argument("--db", default=PROFILE_DB)
    args = ap.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"FATAL: DB not found: {args.db}", file=sys.stderr)
        return 2

    with closing(sqlite3.connect(args.db)) as conn:
        conn.row_factory = sqlite3.Row
        entry = conn.execute(
            "SELECT id, symbol, side, qty, price, status FROM trades WHERE id=?",
            (ENTRY_TRADE_ID,),
        ).fetchone()
        sell = conn.execute(
            "SELECT id, symbol, side, qty, price, status FROM trades WHERE id=?",
            (PENDING_SELL_ID,),
        ).fetchone()

        if entry is not None and entry["status"] == "closed":
            print(f"SKIP: entry #{ENTRY_TRADE_ID} already closed — "
                  f"fix likely already applied")
            return 0
        if not _check(entry, ENTRY_TRADE_ID, "buy", "open"):
            return 2
        if not _check(sell, PENDING_SELL_ID, "sell", "pending_protective"):
            return 2
        if sell["status"] != "pending_protective":
            print(f"FATAL: #{PENDING_SELL_ID} status={sell['status']} "
                  f"(expected pending_protective) — refusing", file=sys.stderr)
            return 2

        realized_pnl = round((FILL_PRICE - float(entry["price"])) * EXPECTED_QTY, 2)
        reason_text = (
            "one-time fix 2026-05-24: trailing-stop replaced by broker; "
            "filled under a different order id so the reconciler could not "
            "match pending row to the fill. Closed manually @ "
            f"${FILL_PRICE:.2f}."
        )

        print()
        print("=== Plan ===")
        print(f"  entry #{ENTRY_TRADE_ID}: NIO buy {EXPECTED_QTY} @ "
              f"${float(entry['price']):.2f} (open -> closed, "
              f"pnl=${realized_pnl:+.2f})")
        print(f"  sell  #{PENDING_SELL_ID}: NIO sell {EXPECTED_QTY} "
              f"(pending_protective -> closed @ ${FILL_PRICE:.2f})")
        print()

        if args.dry_run:
            print("--dry-run: no writes performed")
            return 0

        conn.execute(
            "UPDATE trades SET status='closed', price=?, fill_price=?, "
            "reason=COALESCE(reason || ' | ', '') || ? WHERE id=?",
            (FILL_PRICE, FILL_PRICE, reason_text, PENDING_SELL_ID),
        )
        conn.execute(
            "UPDATE trades SET status='closed', pnl=? WHERE id=?",
            (realized_pnl, ENTRY_TRADE_ID),
        )
        conn.commit()
        print(f"OK: closed pending SELL #{PENDING_SELL_ID} @ ${FILL_PRICE:.2f} "
              f"and entry BUY #{ENTRY_TRADE_ID} (pnl=${realized_pnl:+.2f})")
        print()
        print("Next reconcile cycle should auto-clear the profile's halt.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
