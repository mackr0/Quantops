#!/usr/bin/env python3
"""Repair 2026-07-22 (part 2) — rebuild p212's GOOG 375/380 spread
round trip from broker truth.

What actually happened (established from broker + journals):
  * 07-15: p212 OPENED a bear-call credit spread — sell 3x
    GOOG260724C00375000 (order 8a0a2d3b, filled) + buy 3x
    GOOG260724C00380000 (order 4d12d9f9, filled). REAL fills.
  * The journaling path miscoded those opens as P&L-bearing "close"
    rows (ids 257/258, status pending_fill, booked pnl 2,460 + 2,148
    = 4,608 — computed against a basis that never existed). No open
    rows exist in ANY profile journal; submitted_orders never saw
    them (the option path bypasses the guarded door).
  * The lifecycle later closed the spread at the broker (buy 375C /
    sell 380C — the orders that kept 429-failing until one landed;
    broker is flat both legs today) but the closing fills were never
    journaled in the 429 chaos.
  * Net: real round trip at the broker; the journal carries one
    mis-shaped pair with fabricated pnl. check_decomposition counts
    that pnl as realized -> the constant −4,608 gap -> kill switch.

The repair (broker-verified, dry-run default, refuses on ambiguity):
  1. Pull the account's closed orders and find, for each contract,
     the FILLED opening order (must match ids 8a0a2d3b / 4d12d9f9)
     and the FILLED closing order (opposite side, after 07-15).
  2. Verify broker position is 0 for both contracts.
  3. Rewrite rows 257/258 as consumed OPEN legs: status='closed',
     pnl=NULL, fill_price = their real open fill price.
  4. INSERT the two missing CLOSE rows from the broker closing fills,
     carrying the TRUE per-leg pnl (house convention: pnl lives on
     the close row):
        375C short leg: (open_sell - close_buy) * qty * 100
        380C long  leg: (close_sell - open_buy) * qty * 100
     Idempotent: skips if the closing order_id already has a row.
  If the closing fills cannot be found, or anything is ambiguous, it
  prints what it found and REFUSES to apply. Nothing is guessed.

Run from /opt/quantopsai with the venv python:
    venv/bin/python3 scripts/repair_goog_spread_p212_2026_07_22.py           # dry run
    venv/bin/python3 scripts/repair_goog_spread_p212_2026_07_22.py --apply
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from contextlib import closing

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

PID = 212
DB = f"/opt/quantopsai/quantopsai_profile_{PID}.db"
LEG_375 = "GOOG260724C00375000"
LEG_380 = "GOOG260724C00380000"
OPEN_ROW_375 = 257     # journal row: sell 3 @ 10.05 (the real open)
OPEN_ROW_380 = 258     # journal row: buy 3 @ 8.35 (the real open)
OPEN_OID_375 = "8a0a2d3b"
OPEN_OID_380 = "4d12d9f9"
CONTRACT_MULT = 100


def _filled_orders_for(api, occ):
    """All FILLED orders for one OCC contract, oldest first."""
    out = []
    for o in api.list_orders(status="closed", limit=500, nested=True):
        if (getattr(o, "symbol", "") or "") != occ:
            continue
        if (getattr(o, "status", "") or "").lower() != "filled":
            continue
        out.append(o)
    out.sort(key=lambda o: str(getattr(o, "filled_at", "") or ""))
    return out


def _broker_position_qty(api, occ):
    for p in api.list_positions():
        if (getattr(p, "symbol", "") or "") == occ:
            return float(getattr(p, "qty", 0) or 0)
    return 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    from models import build_user_context_from_profile
    from client import get_api
    api = get_api(build_user_context_from_profile(PID))

    plan = []
    fatal = []

    for occ, open_oid_prefix, open_side, close_side in (
        (LEG_375, OPEN_OID_375, "sell", "buy"),
        (LEG_380, OPEN_OID_380, "buy", "sell"),
    ):
        orders = _filled_orders_for(api, occ)
        print(f"\n{occ}: {len(orders)} filled order(s) at the broker:")
        for o in orders:
            print(f"  {str(o.id)[:8]} {o.side} qty={o.qty} "
                  f"avg={getattr(o, 'filled_avg_price', '?')} "
                  f"filled_at={getattr(o, 'filled_at', '?')}")
        opens = [o for o in orders
                 if str(o.id).startswith(open_oid_prefix)
                 and (o.side or "").lower() == open_side]
        closes = [o for o in orders
                  if (o.side or "").lower() == close_side]
        if len(opens) != 1:
            fatal.append(f"{occ}: expected exactly 1 filled OPEN "
                         f"({open_side}, id {open_oid_prefix}…) — "
                         f"found {len(opens)}")
            continue
        if len(closes) != 1:
            fatal.append(f"{occ}: expected exactly 1 filled CLOSE "
                         f"({close_side}) — found {len(closes)}; "
                         f"cannot attribute unambiguously")
            continue
        pos = _broker_position_qty(api, occ)
        if pos != 0:
            fatal.append(f"{occ}: broker still holds {pos} — the round "
                         f"trip is NOT complete; refusing")
            continue
        plan.append((occ, opens[0], closes[0], open_side, close_side))

    if fatal:
        print("\nREFUSING to apply — broker record does not match the "
              "expected shape:")
        for f in fatal:
            print(f"  {f}")
        sys.exit(1)

    # Build the journal changes.
    changes = []
    for occ, o_open, o_close, open_side, close_side in plan:
        qty = float(o_open.qty)
        open_px = float(o_open.filled_avg_price)
        close_px = float(o_close.filled_avg_price)
        if open_side == "sell":     # short leg: credit in, debit out
            pnl = (open_px - close_px) * qty * CONTRACT_MULT
        else:                        # long leg: debit in, credit out
            pnl = (close_px - open_px) * qty * CONTRACT_MULT
        row_id = OPEN_ROW_375 if occ == LEG_375 else OPEN_ROW_380
        changes.append((occ, row_id, o_open, o_close, qty,
                        open_px, close_px, close_side, pnl))

    total_pnl = sum(c[-1] for c in changes)
    print("\nPLAN:")
    for occ, row_id, o_open, o_close, qty, open_px, close_px, close_side, pnl in changes:
        print(f"  row #{row_id} ({occ}): -> consumed OPEN leg "
              f"(status=closed, pnl=NULL, fill_price={open_px})")
        print(f"  INSERT close row: {close_side} {qty:g} {occ} "
              f"@ {close_px} (order {str(o_close.id)[:8]}) "
              f"pnl={pnl:+,.2f}")
    print(f"  TRUE spread pnl: {total_pnl:+,.2f} "
          f"(replaces the fabricated +4,608)")

    if not args.apply:
        print("\nDRY RUN — re-run with --apply to execute.")
        return

    with closing(sqlite3.connect(DB)) as conn:
        for occ, row_id, o_open, o_close, qty, open_px, close_px, close_side, pnl in changes:
            already = conn.execute(
                "SELECT 1 FROM trades WHERE order_id = ?",
                (str(o_close.id),)).fetchone()
            conn.execute(
                "UPDATE trades SET status='closed', pnl=NULL, "
                "fill_price=? WHERE id = ?", (open_px, row_id))
            if already:
                print(f"close row for {str(o_close.id)[:8]} already "
                      f"journaled — skipping insert (idempotent)")
            else:
                conn.execute(
                    "INSERT INTO trades (timestamp, symbol, side, qty, "
                    " price, fill_price, pnl, status, order_id, "
                    " occ_symbol, signal_type, reason) "
                    "VALUES (?, 'GOOG', ?, ?, ?, ?, ?, 'closed', ?, ?, "
                    " 'MULTILEG', ?)",
                    (str(getattr(o_close, "filled_at", "") or ""),
                     close_side, qty, close_px, close_px, pnl,
                     str(o_close.id), occ,
                     "repair 2026-07-22: closing fill reconstructed "
                     "from broker (was lost in the 429 storm)"))
        conn.commit()
    print("\nAPPLIED. The integrity gate re-checks every cycle; the "
          "kill switch auto-clears once the decomposition gap closes.")


if __name__ == "__main__":
    main()
