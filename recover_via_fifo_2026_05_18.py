"""ONE-OFF: FIFO-aware recovery of wrongly-closed BUY rows.

After the 2026-05-18 race-condition outage, many BUY rows were
marked status='closed' but their lots still had remaining qty
(partial or no sell consumed them). The earlier
`recover_wrongly_closed_buys_2026_05_18.py` used a `pnl IS NULL`
filter which produced false positives (e.g., a BUY fully consumed
by a SELL has pnl=NULL on the BUY because pnl is recorded on the
SELL row — that BUY is legitimately closed, not wrongly closed).

This script does the correct check:
  1. For each profile, walk ALL trades in FIFO order
  2. For each BUY lot, compute remaining qty after all SELL consumption
  3. If status='closed' but remaining qty > 0 → flip to 'open' (wrongly closed)
  4. If status='open' but remaining qty == 0 → flip to 'closed' (wrongly open)

The first case (closed-but-not-empty) is today's bug.
The second case (open-but-empty) is for completeness — should be
empty if no SELL was applied incorrectly.

Run on prod:
    cd /opt/quantopsai && /opt/quantopsai/venv/bin/python \\
        recover_via_fifo_2026_05_18.py --apply
"""
from __future__ import annotations

import argparse
import glob
import os
import sqlite3
import sys
from contextlib import closing


def fifo_recompute(db_path: str):
    """Returns (to_reopen, to_close) — lists of trade ids whose status
    disagrees with the FIFO-correct remaining qty."""
    with closing(sqlite3.connect(db_path)) as conn:
        has_trades = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='trades'"
        ).fetchone()
        if not has_trades:
            return [], []
        # Per-symbol FIFO walk. side='buy' opens a long lot, 'sell'
        # consumes from oldest open lot first. Same convention used by
        # get_virtual_positions and the new fill-confirm logic.
        symbols = [
            r[0] for r in conn.execute(
                "SELECT DISTINCT symbol FROM trades "
                "WHERE side IN ('buy', 'sell') AND symbol IS NOT NULL"
            ).fetchall()
        ]
        to_reopen, to_close = [], []
        for sym in symbols:
            rows = conn.execute(
                "SELECT id, side, qty, status FROM trades "
                "WHERE symbol = ? "
                "  AND COALESCE(status, 'open') != 'canceled' "
                "ORDER BY timestamp ASC, id ASC",
                (sym,),
            ).fetchall()
            lots = []  # [trade_id, qty_remaining, current_status]
            for r in rows:
                tid, side, qty, status = r
                qty = float(qty or 0)
                if side == "buy":
                    lots.append([tid, qty, status or "open"])
                elif side == "sell" and qty > 0:
                    remaining = qty
                    for lot in lots:
                        if remaining <= 0:
                            break
                        if lot[1] <= 0:
                            continue
                        consumed = min(lot[1], remaining)
                        lot[1] -= consumed
                        remaining -= consumed
            for lot_id, lot_remaining, current_status in lots:
                if lot_remaining > 1e-6 and current_status == "closed":
                    to_reopen.append((lot_id, sym, lot_remaining))
                elif lot_remaining <= 1e-6 and current_status == "open":
                    to_close.append((lot_id, sym))
        return to_reopen, to_close


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually write the UPDATEs (default: dry-run)")
    args = ap.parse_args()
    grand_open = grand_close = 0
    for db in sorted(glob.glob("/opt/quantopsai/quantopsai_profile_*.db")):
        pid = os.path.basename(db).split("_")[-1].replace(".db", "")
        to_reopen, to_close = fifo_recompute(db)
        if not to_reopen and not to_close:
            continue
        print(f"\n--- P{pid} ---")
        if to_reopen:
            print(f"  flip {len(to_reopen)} BUYs CLOSED → OPEN (lot has remaining qty):")
            for tid, sym, rem in to_reopen[:10]:
                print(f"    id={tid:>4} {sym:6} remaining_qty={rem}")
            if len(to_reopen) > 10:
                print(f"    ...{len(to_reopen)-10} more")
        if to_close:
            print(f"  flip {len(to_close)} BUYs OPEN → CLOSED (lot fully consumed):")
            for tid, sym in to_close[:10]:
                print(f"    id={tid:>4} {sym:6}")
            if len(to_close) > 10:
                print(f"    ...{len(to_close)-10} more")
        if args.apply:
            with closing(sqlite3.connect(db)) as conn:
                conn.executemany(
                    "UPDATE trades SET status='open' WHERE id=?",
                    [(t[0],) for t in to_reopen],
                )
                conn.executemany(
                    "UPDATE trades SET status='closed' WHERE id=?",
                    [(t[0],) for t in to_close],
                )
                conn.commit()
            print(f"    APPLIED")
        grand_open += len(to_reopen)
        grand_close += len(to_close)
    print()
    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"=== {mode}: {grand_open} reopen, {grand_close} close ===")
    if not args.apply:
        print("Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
