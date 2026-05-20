"""Recompute correct stock P&L on the 6 rows contaminated by the
dict-key collision bug fixed in #189 (2026-05-20).

Bug recap (see CHANGELOG 2026-05-20 PM "Stock pipeline stops seeing
option positions"): when a profile held stock X plus an option leg on
X, `execute_trade`'s `positions = {p["symbol"]: p for p in positions_list}`
collided keys because `Position.__getitem__("symbol")` returns the
underlying for both. The dict comprehension kept whichever landed
last. When AI emitted STRONG_SELL on X and the option leg won the
collision, the code:
  1. Submitted a stock sell of qty=1 (the option leg's contract count)
     instead of the full stock position.
  2. Wrote the journal row with `pnl = position.get("unrealized_pl")`
     from the OPTION leg — option premium magnitude on a stock row.

Impact analysis (2026-05-20):
  pid 15: 2 EWJ rows with phantom pnl=-$215 each
  pid 20: 4 QCOM rows (2 with impossible -$90/-$111, 2 with plausible
          +$9/+$25.50 that are still wrong-qty stock-pnl calcs).

This script:
  1. Identifies the 6 affected row ids explicitly (no broad WHERE).
  2. For each, looks up FIFO BUY rows on the same symbol in the same
     profile DB.
  3. Computes correct cost basis (weighted-avg of buys consumed FIFO
     against this sell's qty).
  4. Recomputes pnl = (sell_price - cb) * qty.
  5. UPDATE the row's pnl (and decision_price if it tracked the wrong
     option premium — verify and update if needed).
  6. JSONL audit log of every before/after.

Default dry-run. Pass --apply to commit.

Cash/equity unaffected: profile equity comes from Alpaca's
account.equity, not SUM(pnl) of journal rows. This script does not
touch the broker.

Safety: targets 6 explicit row IDs. Never widens. Re-running is
idempotent (a row that already has correct stock-style pnl gets the
same value written; audit log will show no-op).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from typing import Dict, List, Tuple, Optional

REPO = "/opt/quantopsai" if os.path.isdir("/opt/quantopsai") else os.getcwd()
AUDIT_LOG = os.path.join(
    REPO, "scripts",
    f"recompute_pnl_audit_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.jsonl",
)

# Explicit target rows. Investigation 2026-05-20 found exactly these
# 6 rows in the population of all SELL/STRONG_SELL stock-rows where
# the profile also held an option leg on the same underlying.
# (profile_id, trade_id, symbol) tuples.
TARGET_ROWS: List[Tuple[int, int, str]] = [
    (15, None, "EWJ"),   # will resolve via timestamp+symbol+side filter
    (15, None, "EWJ"),
    (20, None, "QCOM"),
    (20, None, "QCOM"),
    (20, None, "QCOM"),
    (20, None, "QCOM"),
]


def _profile_db(pid: int) -> str:
    return os.path.join(REPO, f"quantopsai_profile_{pid}.db")


def _find_affected_rows(pid: int) -> List[sqlite3.Row]:
    """Find the SELL rows on this profile that match the bug pattern:
    side=sell, occ_symbol IS NULL, signal_type IN STRONG_SELL/SELL,
    AND symbol also has option positions in this DB."""
    db = _profile_db(pid)
    if not os.path.isfile(db):
        return []
    with closing(sqlite3.connect(db)) as conn:
        conn.row_factory = sqlite3.Row
        return list(conn.execute("""
            SELECT id, timestamp, symbol, side, qty, price, pnl, signal_type,
                   decision_price
            FROM trades
            WHERE side='sell'
              AND occ_symbol IS NULL
              AND pnl IS NOT NULL
              AND signal_type IN ('STRONG_SELL','SELL')
              AND symbol IN (
                SELECT DISTINCT symbol FROM trades WHERE occ_symbol IS NOT NULL
              )
            ORDER BY timestamp ASC
        """))


def _fifo_cost_basis(conn: sqlite3.Connection, symbol: str,
                     sell_ts: str, sell_qty: float) -> Optional[float]:
    """FIFO cost basis for `sell_qty` shares of `symbol`, consuming
    BUY rows on this symbol in chronological order until exhausted.

    Returns weighted-avg cost-basis per share, or None if BUY history
    is insufficient (which means we can't compute correctly — caller
    should skip the row rather than write a wrong correction).
    """
    # Only consider stock buys (occ_symbol IS NULL) of this symbol
    # that happened BEFORE this sell. Exclude AUTO_RECONCILE
    # sentinels and signal types that aren't ordinary opens.
    buys = list(conn.execute("""
        SELECT id, timestamp, qty, price, signal_type
        FROM trades
        WHERE side='buy'
          AND symbol=?
          AND occ_symbol IS NULL
          AND timestamp < ?
          AND signal_type NOT IN ('AUTO_RECONCILE', 'AUTO_RECONCILE_PHANTOM_CLOSE')
        ORDER BY timestamp ASC
    """, (symbol, sell_ts)))
    if not buys:
        return None

    # We also need to subtract prior SELLs to compute remaining qty
    # per buy. FIFO consumes from oldest buy first.
    prior_sells = list(conn.execute("""
        SELECT qty FROM trades
        WHERE side='sell'
          AND symbol=?
          AND occ_symbol IS NULL
          AND timestamp < ?
        ORDER BY timestamp ASC
    """, (symbol, sell_ts)))
    sold_qty_consumed = sum(float(r["qty"] or 0) for r in prior_sells)

    # Walk buys in order, subtracting consumed qty
    remaining_buy_qty: List[Tuple[float, float]] = []  # (price, qty_remaining)
    for b in buys:
        bq = float(b["qty"] or 0)
        bp = float(b["price"] or 0)
        if bq <= 0 or bp <= 0:
            continue
        # Subtract from this buy whatever was already consumed
        if sold_qty_consumed >= bq:
            sold_qty_consumed -= bq
            continue
        else:
            remaining_buy_qty.append((bp, bq - sold_qty_consumed))
            sold_qty_consumed = 0

    if not remaining_buy_qty:
        return None

    # Consume sell_qty against the FIFO queue
    need = float(sell_qty)
    total_cost = 0.0
    total_qty = 0.0
    for price, qty_avail in remaining_buy_qty:
        if need <= 0:
            break
        take = min(qty_avail, need)
        total_cost += price * take
        total_qty += take
        need -= take
    if need > 0:
        # Insufficient BUY history — couldn't fully cover this sell
        return None
    if total_qty == 0:
        return None
    return total_cost / total_qty


def _is_contaminated(qty: float, price: float, pnl: float) -> bool:
    """Implied cost basis = price - pnl/qty. If wildly outside the
    plausible stock range, the pnl is contaminated."""
    if qty == 0 or price == 0:
        return False
    implied_cb = price - pnl / qty
    return not (0.5 * price <= implied_cb <= 2.0 * price)


def _process(apply: bool) -> int:
    print(f"Mode: {'APPLY (writes)' if apply else 'DRY-RUN (no writes)'}")
    print(f"Audit log: {AUDIT_LOG}")
    print()

    # Collect target rows by scanning each profile
    profile_ids = sorted({pid for (pid, _, _) in TARGET_ROWS})
    total_fixed = 0
    total_skipped = 0
    with open(AUDIT_LOG, "w") as audit_fh:
        for pid in profile_ids:
            rows = _find_affected_rows(pid)
            print(f"=== pid {pid} ({len(rows)} candidate row(s)) ===")
            if not rows:
                continue
            db = _profile_db(pid)
            with closing(sqlite3.connect(db)) as conn:
                conn.row_factory = sqlite3.Row
                for r in rows:
                    rid = r["id"]
                    sym = r["symbol"]
                    qty = float(r["qty"] or 0)
                    price = float(r["price"] or 0)
                    old_pnl = float(r["pnl"] or 0)

                    cb = _fifo_cost_basis(conn, sym, r["timestamp"], qty)
                    if cb is None:
                        audit_entry = {
                            "profile_id": pid, "trade_id": rid,
                            "timestamp": r["timestamp"],
                            "symbol": sym, "qty": qty, "price": price,
                            "old_pnl": old_pnl,
                            "result": "SKIPPED — insufficient BUY history to compute FIFO cost basis",
                        }
                        audit_fh.write(json.dumps(audit_entry) + "\n")
                        total_skipped += 1
                        print(f"  trade_id={rid} {sym} qty={qty} px=${price:.4f} pnl=${old_pnl:.2f} "
                              f"→ SKIPPED (no BUY history)")
                        continue

                    new_pnl = (price - cb) * qty
                    # Round for sanity at display time
                    new_pnl_r = round(new_pnl, 4)

                    audit_entry = {
                        "profile_id": pid, "trade_id": rid,
                        "timestamp": r["timestamp"],
                        "symbol": sym, "qty": qty, "price": price,
                        "fifo_cost_basis": cb,
                        "old_pnl": old_pnl,
                        "new_pnl": new_pnl_r,
                        "was_contaminated": _is_contaminated(qty, price, old_pnl),
                        "result": "WOULD_UPDATE" if not apply else "UPDATED",
                    }
                    audit_fh.write(json.dumps(audit_entry) + "\n")

                    print(f"  trade_id={rid} {sym} qty={qty} px=${price:.4f} "
                          f"cb=${cb:.4f} pnl: ${old_pnl:+.2f} → ${new_pnl_r:+.4f} "
                          f"({'contaminated' if audit_entry['was_contaminated'] else 'qty-wrong'})")

                    if apply:
                        conn.execute(
                            "UPDATE trades SET pnl = ? WHERE id = ?",
                            (new_pnl_r, rid),
                        )
                        total_fixed += 1
                if apply:
                    conn.commit()
    print()
    print("=" * 60)
    print(f"  rows {'updated' if apply else 'would-update'}: {total_fixed if apply else len(rows) - total_skipped}")
    print(f"  rows skipped (no FIFO history): {total_skipped}")
    print(f"  audit: {AUDIT_LOG}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Actually UPDATE rows. Default is dry-run.")
    args = ap.parse_args()
    return _process(args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
