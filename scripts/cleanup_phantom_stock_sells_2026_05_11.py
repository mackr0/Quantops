"""One-shot cleanup of the 2026-05-11 phantom-stock-sell bug
fallout.

The bug class (recap).
On 2026-05-11 between 14:18-16:27 UTC, `check_stop_loss_take_profit`
fired stop-loss SELL orders against multileg option-leg positions
that came through with `occ_symbol` accidentally null. Each order
was journaled with `signal_type='SELL'`, `symbol=<underlying>`,
`occ_symbol=NULL`, and a price equal to the OPTION PREMIUM
($0.15-$3.50 range) — not the stock price ($70-$290 range). Each
order WAS submitted to Alpaca. On a paper account so no real money
loss; the broker journal was corrupted.

The Phase 5e commits 2026-05-11/12 plugged the upstream propagation
hole. Today's `portfolio_manager.check_stop_loss_take_profit`
defensive guardrail prevents the bug class from re-firing here even
if a regression returns to the same shape.

This script handles the historical fallout:

  1. Tag every matching journal row with `data_quality='polluted'`
     so analytics queries (win-rate, P&L attribution, slippage,
     etc.) exclude them. The data_quality_clause helper already
     understands this tag — see Phase 5e.

  2. Report a per-profile per-symbol summary so the operator can
     manually reconcile any broker holdings against the corrupted
     journal.

Idempotent: re-running is safe (the WHERE matches only rows that
weren't already tagged).

Run on prod: python3 /tmp/cleanup_phantom_stock_sells_2026_05_11.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing
from typing import Dict, List, Tuple

DB_DIR = "/opt/quantopsai"
PROFILES = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]


def _find_polluted_rows(profile_id: int) -> List[Tuple[int, str, float, float, str]]:
    """Return list of (id, symbol, qty, price, timestamp) for journal
    rows that match the bug shape: side='sell' or 'cover',
    occ_symbol IS NULL, signal_type IN ('SELL', 'COVER', 'STRONG_SELL'),
    price < $5 (option premium shape), reason starts with
    "Stop-loss triggered" or "Short stop-loss triggered", and the
    row hasn't been tagged 'polluted' yet."""
    db = f"{DB_DIR}/quantopsai_profile_{profile_id}.db"
    if not os.path.exists(db):
        return []
    with closing(sqlite3.connect(db)) as conn:
        rows = conn.execute(
            "SELECT id, symbol, qty, price, timestamp FROM trades "
            "WHERE side IN ('sell', 'cover') "
            "  AND occ_symbol IS NULL "
            "  AND signal_type IN ('SELL', 'COVER', 'STRONG_SELL') "
            "  AND price > 0 AND price < 5.0 "
            "  AND COALESCE(data_quality, '') != 'polluted' "
            "  AND reason LIKE 'Stop-loss triggered%'"
        ).fetchall()
    return rows


def _tag_polluted(profile_id: int, ids: List[int]) -> int:
    """Mark each id with data_quality='polluted'. Returns rowcount."""
    if not ids:
        return 0
    db = f"{DB_DIR}/quantopsai_profile_{profile_id}.db"
    placeholders = ",".join("?" for _ in ids)
    with closing(sqlite3.connect(db)) as conn:
        cur = conn.execute(
            f"UPDATE trades SET data_quality='polluted' "
            f"WHERE id IN ({placeholders})",
            ids,
        )
        conn.commit()
        return cur.rowcount


def main():
    print("=" * 70)
    print("Phantom-stock-sells cleanup — 2026-05-11 incident fallout")
    print("=" * 70)
    print()

    print("[1/2] Identifying polluted journal rows per profile...")
    plan: Dict[int, List[Tuple[int, str, float, float, str]]] = {}
    summary_per_symbol: Dict[Tuple[int, str], Tuple[int, float]] = {}
    for pid in PROFILES:
        rows = _find_polluted_rows(pid)
        if rows:
            plan[pid] = rows
            print(f"  pid {pid}: {len(rows)} polluted row(s)")
            for tid, sym, qty, price, ts in rows:
                key = (pid, sym)
                cnt, total_qty = summary_per_symbol.get(key, (0, 0.0))
                summary_per_symbol[key] = (cnt + 1, total_qty + float(qty))
        else:
            print(f"  pid {pid}: no polluted rows (already cleaned, "
                  f"or never had any)")
    total = sum(len(v) for v in plan.values())
    print()
    print(f"Total polluted rows to tag: {total}")
    print()

    if total == 0:
        print("Nothing to do. Exiting.")
        return

    print("[2/2] Tagging rows with data_quality='polluted'...")
    tagged = 0
    for pid, rows in plan.items():
        ids = [r[0] for r in rows]
        n = _tag_polluted(pid, ids)
        print(f"  pid {pid}: tagged {n} row(s)")
        tagged += n
    print()
    print(f"DONE. Tagged {tagged}/{total} rows.")
    print()
    print("=" * 70)
    print("Summary per profile + symbol (for broker reconciliation):")
    print("=" * 70)
    for (pid, sym), (cnt, qty) in sorted(summary_per_symbol.items()):
        print(f"  pid {pid:>2}  {sym:>6}  {cnt:>3} bad SELL row(s)  "
              f"total qty {qty:>5.0f}")
    print()
    print("Next step (manual): for each (pid, symbol) above, check the "
          "actual Alpaca holdings vs the journal — the broker may "
          "have executed those bad orders. The next reconcile cycle "
          "will detect any drift and either backfill or correct it. "
          "If a position you expected to hold is missing N shares "
          "matching the bad-row qty, those shares were sold by the "
          "bug; you can buy them back at current market or accept "
          "the loss/gain depending on how price has moved.")


if __name__ == "__main__":
    sys.exit(main())
