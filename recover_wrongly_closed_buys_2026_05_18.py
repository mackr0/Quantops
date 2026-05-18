"""ONE-OFF: recover BUY rows that the pre-fix reconcile_trade_statuses
wrongly marked status='closed' during the 2026-05-18 13:30 ET market
open.

Criterion for "wrongly closed":
  - side='buy'
  - status='closed'
  - has a real Alpaca order_id (UUID-shaped)
  - timestamp >= 2026-05-18 13:30:00 (today, post-open)
  - pnl IS NULL — the buggy step 2 of reconcile_trade_statuses
    flips status without writing pnl, whereas the legitimate close
    path (FIFO matching in step 3) writes realized pnl onto the
    BUY row. A status='closed' BUY without pnl was therefore closed
    by the bug, not by a real exit.

Flip those back to status='open' so get_virtual_positions can see
them and the dashboard reflects real held capital.

Run on prod:
    cd /opt/quantopsai && set -a; . ./.env; set +a; \
        /opt/quantopsai/venv/bin/python recover_wrongly_closed_buys_2026_05_18.py --apply

Without --apply this is a DRY-RUN that prints what it would change
without writing.
"""
from __future__ import annotations

import argparse
import glob
import os
import sqlite3
import sys
from contextlib import closing

CUTOFF = "2026-05-18T13:30:00"

# A UUID is 36 chars with dashes at fixed positions. Stricter than
# "non-empty" so we don't accidentally resurrect rows whose order_id
# is a placeholder / test value.
import re
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _is_real_order_id(s: str | None) -> bool:
    return bool(s) and bool(_UUID_RE.match(s))


def candidates(db_path: str) -> list[tuple]:
    """Return wrongly-closed BUY rows for this profile DB. Skips DBs
    that don't have a trades table (orphan shells from deleted
    profiles)."""
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        has_trades = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='trades'"
        ).fetchone()
        if not has_trades:
            return []
        rows = conn.execute(
            "SELECT id, timestamp, symbol, qty, price, order_id "
            "FROM trades "
            "WHERE side='buy' AND status='closed' "
            "  AND pnl IS NULL "
            "  AND timestamp >= ? "
            "ORDER BY timestamp",
            (CUTOFF,),
        ).fetchall()
    return [tuple(r) for r in rows if _is_real_order_id(r["order_id"])]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--apply", action="store_true",
        help="Actually write the UPDATE. Without this flag, dry-run.",
    )
    ap.add_argument(
        "--db-glob", default="/opt/quantopsai/quantopsai_profile_*.db",
        help="Glob for profile DBs to scan",
    )
    args = ap.parse_args()

    total_flipped = 0
    affected_dbs = 0
    for db in sorted(glob.glob(args.db_glob)):
        pid = os.path.basename(db).split("_")[-1].replace(".db", "")
        cands = candidates(db)
        if not cands:
            continue
        affected_dbs += 1
        print(f"--- P{pid} ({db}) — {len(cands)} wrongly-closed BUYs ---")
        for r in cands[:5]:
            print(f"    id={r[0]:>5}  {r[1]}  {r[2]:6}  qty={r[3]}  px=${r[4]:.2f}  order_id={r[5][:8]}…")
        if len(cands) > 5:
            print(f"    ...{len(cands)-5} more")
        if args.apply:
            with closing(sqlite3.connect(db)) as conn:
                conn.executemany(
                    "UPDATE trades SET status='open' WHERE id=?",
                    [(r[0],) for r in cands],
                )
                conn.commit()
            print(f"    FLIPPED {len(cands)} → status='open'")
        total_flipped += len(cands)

    print()
    print("=" * 60)
    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"{mode}: {total_flipped} BUYs across {affected_dbs} profile DBs")
    if not args.apply:
        print("Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
