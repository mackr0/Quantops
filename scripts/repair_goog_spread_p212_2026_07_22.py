#!/usr/bin/env python3
"""Repair 2026-07-22 (part 2, v2) — rebuild p212's GOOG 375/380 spread
round trip; the v1 dry run recovered the full true history.

Established, broker-verified:
  * 07-15 19:02 — p212 OPENED a bear-call credit spread: sell 3x
    GOOG260724C00375000 @ 10.05, buy 3x GOOG260724C00380000 @ 8.35
    (net credit $510). Journal rows 257/258 ARE those opens — real.
  * The lifecycle's close (buy 375C / sell 380C) 429-failed for a week,
    then FILLED 2026-07-22 13:45: buy 375C @ 1.85 (order 8a0a2d3b…),
    sell 380C @ 1.19 (order 4d12d9f9…). Close debit $198. Broker flat.
  * TRUE spread pnl: (10.05−1.85)·300 − (8.35−1.19)·300 = 2,460 −
    2,148 = +$312.
  * The close bookkeeping wrote onto the OPEN rows instead of creating
    close rows: it overwrote their order_ids with the close order ids
    and stamped per-leg pnl WITH THE LONG LEG'S SIGN INVERTED
    (+2,148 instead of −2,148) — booking +4,608 phantom realized pnl,
    which is the integrity gate's constant −4,608 decomposition gap.

The repair (dry-run default; every fact re-verified before applying):
  1. get_order on the two close ids taken from rows 257/258 —
     must be status=filled, sides buy(375C)/sell(380C), qty 3.
  2. Broker position must be flat for both contracts.
  3. Rows 257/258 must still be status='pending_fill' with the open
     premiums (10.05 / 8.35) — i.e. the incident state; anything else
     means the state moved and the script refuses.
  4. Apply: rows 257/258 become consumed OPEN legs (status='closed',
     pnl=NULL, fill_price=open premium, order_id='REPAIR-OPEN-…' so
     the close ids belong solely to the close rows); INSERT the two
     close rows from the broker fills with the TRUE per-leg pnl
     (+2,460 short leg / −2,148 long leg — pnl on close rows, house
     convention). Idempotent: refuses if already applied.

Run from /opt/quantopsai:
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
CONTRACT_MULT = 100

# (row_id, occ, open_side, open_premium, close_side)
LEGS = (
    (257, "GOOG260724C00375000", "sell", 10.05, "buy"),
    (258, "GOOG260724C00380000", "buy", 8.35, "sell"),
)


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

    fatal = []
    plan = []

    with closing(sqlite3.connect(DB)) as conn:
        conn.row_factory = sqlite3.Row
        for row_id, occ, open_side, open_px, close_side in LEGS:
            r = conn.execute(
                "SELECT id, side, qty, price, pnl, status, order_id "
                "FROM trades WHERE id = ?", (row_id,)).fetchone()
            if r is None:
                fatal.append(f"row #{row_id} missing")
                continue
            if r["status"] != "pending_fill":
                fatal.append(
                    f"row #{row_id} status={r['status']!r} (expected "
                    f"'pending_fill') — state moved; refusing")
                continue
            if (r["side"] or "").lower() != open_side or \
                    abs(float(r["price"] or 0) - open_px) > 0.001:
                fatal.append(
                    f"row #{row_id} side/price {r['side']}/{r['price']} "
                    f"!= expected {open_side}/{open_px}; refusing")
                continue
            close_oid = r["order_id"]
            try:
                order = api.get_order(close_oid)
            except Exception as exc:
                fatal.append(f"{occ}: get_order({str(close_oid)[:8]}) "
                             f"failed: {exc}")
                continue
            o_status = (getattr(order, "status", "") or "").lower()
            o_side = (getattr(order, "side", "") or "").lower()
            o_qty = float(getattr(order, "qty", 0) or 0)
            o_sym = getattr(order, "symbol", "") or ""
            close_px = float(getattr(order, "filled_avg_price", 0) or 0)
            if (o_status != "filled" or o_side != close_side
                    or o_qty != float(r["qty"]) or o_sym != occ
                    or close_px <= 0):
                fatal.append(
                    f"{occ}: broker order {str(close_oid)[:8]} is "
                    f"{o_side} {o_qty} {o_sym} status={o_status} "
                    f"avg={close_px} — does not match the expected "
                    f"CLOSE fill; refusing")
                continue
            pos = _broker_position_qty(api, occ)
            if pos != 0:
                fatal.append(f"{occ}: broker still holds {pos}; refusing")
                continue
            qty = float(r["qty"])
            if open_side == "sell":     # short leg
                pnl = (open_px - close_px) * qty * CONTRACT_MULT
            else:                        # long leg
                pnl = (close_px - open_px) * qty * CONTRACT_MULT
            plan.append((row_id, occ, open_side, open_px, close_side,
                         close_px, qty, str(close_oid),
                         str(getattr(order, "filled_at", "") or ""), pnl))

        if fatal:
            print("REFUSING — facts do not match the established "
                  "history:")
            for f in fatal:
                print(f"  {f}")
            sys.exit(1)

        total = sum(p[-1] for p in plan)
        print("PLAN:")
        for (row_id, occ, open_side, open_px, close_side, close_px,
             qty, close_oid, filled_at, pnl) in plan:
            print(f"  row #{row_id}: consumed OPEN {open_side} {qty:g} "
                  f"{occ} @ {open_px} (status=closed, pnl=NULL, "
                  f"order_id=REPAIR-OPEN-{row_id})")
            print(f"  INSERT close: {close_side} {qty:g} {occ} @ "
                  f"{close_px} order={close_oid[:8]} "
                  f"filled={filled_at[:19]} pnl={pnl:+,.2f}")
        print(f"  TRUE spread pnl {total:+,.2f} replaces the fabricated "
              f"+4,608 (long-leg sign was inverted).")

        if not args.apply:
            print("\nDRY RUN — re-run with --apply to execute.")
            return

        for (row_id, occ, open_side, open_px, close_side, close_px,
             qty, close_oid, filled_at, pnl) in plan:
            conn.execute(
                "UPDATE trades SET status='closed', pnl=NULL, "
                "fill_price=?, order_id=? WHERE id = ?",
                (open_px, f"REPAIR-OPEN-{row_id}", row_id))
            conn.execute(
                "INSERT INTO trades (timestamp, symbol, side, qty, "
                " price, fill_price, pnl, status, order_id, occ_symbol, "
                " signal_type, reason) "
                "VALUES (?, 'GOOG', ?, ?, ?, ?, ?, 'closed', ?, ?, "
                " 'MULTILEG', ?)",
                (filled_at, close_side, qty, close_px, close_px, pnl,
                 close_oid, occ,
                 "repair 2026-07-22: closing fill reconstructed from "
                 "broker; open row had been clobbered by the close "
                 "bookkeeping (order_id overwrite + inverted long-leg "
                 "pnl sign)"))
        conn.commit()
    print("\nAPPLIED. Integrity gate re-checks every cycle; the kill "
          "switch auto-clears once the decomposition gap closes.")


if __name__ == "__main__":
    main()
