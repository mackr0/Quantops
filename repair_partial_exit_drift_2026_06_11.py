"""Repair partial-exit drift (2026-06-11).

What happened
-------------
Polling trailing stops fired with a pre-entry high-water mark
(design bug, fixed in portfolio_manager the same day), submitting
full-qty sells against bracket-reserved shares. The broker filled
only the available portion; the entry row had already been flipped
'closed' at submit (position_closed); fix_partial_sell later
corrected the SELL row's qty but nothing reopened the entry. The
unsold remainder stayed at the broker while vanishing from the
virtual book — p97 lost $24.6K of book value across PLUG / SMCI /
NU / IONZ in one session.

What this does
--------------
Per enabled profile, per stock symbol:
  should_hold = Σ filled entry qty (buy rows, open or closed)
              − Σ confirmed sell qty (closed sells)
  book        = get_virtual_positions() net qty
  deficit     = should_hold − book

When deficit > 0 AND the most recent CLOSED buy entry for the
symbol has qty ≥ deficit, that entry is the partial-exit victim:
reopen it (status → 'open'). The FIFO book then nets entry qty
minus the corrected sells = exactly the remainder. Refuses any
shape that doesn't match; idempotent (a reopened entry yields
deficit 0 on the next run).

The OPPOSITE drift (virtual > broker, e.g. BATL +16,419 phantom
virtual shares from partially-filled DAY entry orders) is NOT
repaired here — update_fills' new qty-truth corrects those rows
once the broker orders reach a terminal state (EOD at the latest).

Run:
    venv/bin/python repair_partial_exit_drift_2026_06_11.py            # dry-run
    venv/bin/python repair_partial_exit_drift_2026_06_11.py --apply
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing

REPO_ROOT = "/opt/quantopsai"

_EXCLUDED = ("'pending_protective', 'canceled', 'expired', "
             "'rejected', 'done_for_day', "
             "'auto_reconciled_phantom_close'")


def repair_profile(pid: int, apply: bool) -> int:
    sys.path.insert(0, REPO_ROOT)
    from journal import get_virtual_positions
    db = f"{REPO_ROOT}/quantopsai_profile_{pid}.db"
    book = {}
    for p in get_virtual_positions(db):
        if not p.get("occ_symbol"):
            book[p["symbol"]] = book.get(p["symbol"], 0.0) + float(p["qty"])
    fixed = 0
    with closing(sqlite3.connect(db)) as conn:
        conn.row_factory = sqlite3.Row
        syms = [r[0] for r in conn.execute(
            "SELECT DISTINCT symbol FROM trades WHERE occ_symbol IS NULL"
        ).fetchall()]
        for sym in syms:
            # Net-SIGNED expectation: buys and covers add, sells and
            # shorts subtract — so open shorts net out instead of
            # producing a false positive deficit (p93's NU short
            # showed as deficit 1065 under the buy/sell-only
            # formula).
            should = conn.execute(
                f"SELECT COALESCE(SUM(CASE "
                f"  WHEN side IN ('buy','cover') THEN qty "
                f"  WHEN side IN ('sell','short') THEN -qty "
                f"  ELSE 0 END), 0) "
                f"FROM trades WHERE symbol=? AND occ_symbol IS NULL "
                f"AND side IN ('buy','sell','short','cover') "
                f"AND COALESCE(status,'open') NOT IN ({_EXCLUDED})",
                (sym,),
            ).fetchone()[0]
            deficit = float(should or 0) - float(book.get(sym, 0.0))
            if deficit <= 0.5:
                continue
            # SIMULATE-AND-VERIFY. The virtual book's FIFO excludes
            # CLOSED entries from lots entirely, so the repair goal
            # is: reopen the closed buy entry whose restoration
            # makes the simulated book equal `should` (the
            # confirmed signed net, which matches the broker's
            # per-profile expectation). Lot-identity replays
            # mis-attribute when later round-trips traded the same
            # symbol (p97 NU: the remainder "lives" in an open lot
            # under full-history FIFO, but the BOOK can only be
            # fixed by restoring the closed entry's capacity).
            rows = conn.execute(
                f"SELECT id, side, qty, status FROM trades "
                f"WHERE symbol=? AND occ_symbol IS NULL "
                f"AND side IN ('buy','sell') "
                f"AND COALESCE(status,'open') NOT IN ({_EXCLUDED}) "
                f"ORDER BY timestamp ASC, id ASC",
                (sym,),
            ).fetchall()

            def _sim_book(reopened_id=None):
                """Book-FIFO semantics: closed entries contribute no
                lot (unless it's the candidate being reopened);
                closed sells still consume."""
                lots = []
                for r in rows:
                    q = float(r["qty"] or 0)
                    if q <= 0:
                        continue
                    if r["side"] == "buy":
                        if (r["status"] != "closed"
                                or r["id"] == reopened_id):
                            lots.append(q)
                    else:
                        remaining = q
                        for i in range(len(lots)):
                            if remaining <= 0:
                                break
                            consumed = min(lots[i], remaining)
                            lots[i] -= consumed
                            remaining -= consumed
                return sum(lots)

            candidates = [r for r in rows
                          if r["side"] == "buy"
                          and r["status"] == "closed"]
            chosen = None
            for cand in reversed(candidates):  # newest first
                if abs(_sim_book(cand["id"])
                       - float(should or 0)) <= 1.0:
                    chosen = cand
                    break
            if chosen is None:
                print(f"  p{pid} {sym}: deficit {deficit:.0f} — no "
                      "single closed entry restores book==expected "
                      "— REFUSING (investigate)")
                continue
            print(f"  p{pid} {sym}: "
                  f"{'REOPEN' if apply else 'would reopen'} entry "
                  f"#{chosen['id']} (qty {chosen['qty']:.0f}) — "
                  f"book {float(book.get(sym, 0.0)):.0f} -> "
                  f"{float(should or 0):.0f}")
            if apply:
                conn.execute(
                    "UPDATE trades SET status='open', "
                    "reason=COALESCE(reason || ' | ', '') || ? "
                    "WHERE id=?",
                    ("repair_partial_exit_drift_2026_06_11: "
                     f"reopened — partial exits left {deficit:.0f} "
                     "shares at the broker with no book entry",
                     chosen["id"]),
                )
                fixed += 1
        if apply:
            conn.commit()
    return fixed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    sys.path.insert(0, REPO_ROOT)
    from models import get_active_profile_ids
    total = 0
    for pid in get_active_profile_ids():
        total += repair_profile(pid, args.apply)
    print(f"{'APPLIED' if args.apply else 'DRY-RUN'} — "
          f"{total} entr{'y' if total == 1 else 'ies'} reopened")
    return 0


if __name__ == "__main__":
    sys.exit(main())
