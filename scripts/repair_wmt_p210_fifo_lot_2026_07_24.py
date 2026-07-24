"""Repair p210's WMT lot #271 — consumed at the broker, left open by the
phantom-lot FIFO bug (fixed same day in multi_scheduler).

What happened (2026-07-23, broker-verified): p210 bought 49 WMT (#271,
order cc00dd1b, filled @108.01), sold 49 (#275, order 37df7c91, filled
@107.92), bought 49 again (#276, order eb1760cb, filled @107.90). When
update_fills confirmed #275, the FIFO walk fed its 49 shares into the
EXPIRED, never-filled 79-share protective TP #125 (fill_price NULL)
instead of the real lot #271 — so #271 stayed 'open' and the aggregate
audit read journal +98 vs broker +49 (journal_phantom, acct 55).

This script closes #271 with a signed reason, applying exactly the
verdict the FIXED walk computes. Broker-verified before writing:
  - #275's sell order must be status=filled, side=sell, filled_qty 49
  - #271 must still be status='open', qty 49, order cc00dd1b
  - the fixed _fifo_lots_fully_consumed over the live scope must
    return exactly [271]
Refuses (no write) if any check fails. DRY-RUN by default; --apply to
write.

Usage:
    venv/bin/python scripts/repair_wmt_p210_fifo_lot_2026_07_24.py
    venv/bin/python scripts/repair_wmt_p210_fifo_lot_2026_07_24.py --apply
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir)))

PROFILE_ID = 210
DB = f"quantopsai_profile_{PROFILE_ID}.db"
LOT_ID = 271
LOT_ORDER = "cc00dd1b"
SELL_ID = 275
SELL_ORDER_PREFIX = "37df7c91"
QTY = 49.0

SIGNED_REASON = (
    "closed by repair_wmt_p210_fifo_lot_2026_07_24: lot fully consumed "
    "by confirmed exit row #275 (sell WMT, order 37df7c91 filled 49 "
    "@107.92) — the pre-fix FIFO walk fed that sell into expired "
    "phantom lot #125 instead; broker-verified, applying the fixed "
    "walk's verdict"
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually write (default: dry-run)")
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    try:
        conn.row_factory = sqlite3.Row
        return _run(conn, args)
    finally:
        conn.close()


def _run(conn, args):
    # --- journal preconditions -----------------------------------------
    lot = conn.execute(
        "SELECT id, side, qty, status, order_id, fill_price FROM trades "
        "WHERE id=?", (LOT_ID,)).fetchone()
    if not lot:
        print(f"REFUSE: row #{LOT_ID} not found"); return 1
    if (lot["status"] or "open") != "open" or lot["side"] != "buy" \
            or float(lot["qty"]) != QTY \
            or not str(lot["order_id"]).startswith(LOT_ORDER):
        print(f"REFUSE: row #{LOT_ID} shape changed: "
              f"side={lot['side']} qty={lot['qty']} "
              f"status={lot['status']} order={lot['order_id']}")
        return 1
    sell = conn.execute(
        "SELECT id, side, qty, status, order_id, fill_price FROM trades "
        "WHERE id=?", (SELL_ID,)).fetchone()
    if not sell or sell["side"] != "sell" or float(sell["qty"]) != QTY \
            or sell["status"] != "closed" or sell["fill_price"] is None:
        print(f"REFUSE: sell row #{SELL_ID} shape changed"); return 1

    # --- broker verification -------------------------------------------
    from models import build_user_context_from_profile
    ctx = build_user_context_from_profile(PROFILE_ID)
    api = ctx.get_alpaca_api()
    order = api.get_order(sell["order_id"])
    o_status = str(getattr(order, "status", ""))
    o_side = str(getattr(order, "side", ""))
    o_filled = float(getattr(order, "filled_qty", 0) or 0)
    print(f"broker order {str(sell['order_id'])[:8]}: status={o_status} "
          f"side={o_side} filled_qty={o_filled}")
    if o_status != "filled" or o_side != "sell" or o_filled != QTY:
        print("REFUSE: broker does not confirm the consuming sell")
        return 1

    # --- the fixed walk must agree -------------------------------------
    from multi_scheduler import _fifo_lots_fully_consumed
    rows = conn.execute(
        "SELECT id, side, qty, fill_price FROM trades "
        "WHERE symbol='WMT' AND side IN ('buy','sell') "
        "AND occ_symbol IS NULL "
        "AND COALESCE(status,'open') NOT IN ('canceled','expired') "
        "ORDER BY timestamp ASC, id ASC").fetchall()
    verdict = _fifo_lots_fully_consumed(
        [(r["id"], r["side"], r["qty"], r["fill_price"]) for r in rows],
        "buy")
    print(f"fixed walk verdict: {verdict}")
    if verdict != [LOT_ID]:
        print("REFUSE: the fixed walk does not name exactly this lot")
        return 1

    if not args.apply:
        print(f"DRY-RUN: would close #{LOT_ID} with signed reason. "
              f"Re-run with --apply.")
        return 0

    conn.execute(
        "UPDATE trades SET status='closed', "
        "reason = COALESCE(reason || ' | ', '') || ? "
        "WHERE id=? AND COALESCE(status,'open')='open'",
        (SIGNED_REASON, LOT_ID))
    conn.commit()
    row = conn.execute(
        "SELECT status FROM trades WHERE id=?", (LOT_ID,)).fetchone()
    print(f"APPLIED: #{LOT_ID} status={row['status']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
