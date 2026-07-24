"""Repair p212's CVX 180C expiry liquidation — the missing $2,840 cash flow.

Root cause of the acct-56 cash_parity drift (constant −$2,852.25,
journal HIGH): p212 sold 4x CVX260717C00180000 @1.04 on 07-09 (row #91,
+$416 premium). At 07-17 expiry the call was ITM (CVX ≈ $187.10) and the
broker auto-liquidated the short: BUY 4 @ $7.10 (order 3b5758a0…,
19:30:42Z — broker FILL activity verified). The pre-2026-07-20
assignment sweep flipped #91 to 'closed' but journaled NO row for the
buy-back, so the −$2,840 cash out never entered the virtual books and
#91 kept a fabricated +416 pnl (truth: −(7.10−1.04)×400 = −2,424).
The 2026-07-20 broker-activities capture prevents recurrence; this
repairs the residual damage.

Writes (with --apply):
  1. INSERT the missing buy-to-close row (broker order id, fill 7.10,
     status 'closed', activity-capture conventions, pnl −2,424 via the
     house sign convention, signed reason).
  2. NULL #91's wrong-side pnl (+416 on a sell-to-open row; pnl lives
     on close rows) with a signed reason.

Refuses unless: broker order 3b5758a0 is filled/buy/4 @7.10, row #91
still matches its recorded shape, and NO profile already journals the
order (idempotent). DRY-RUN by default.
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir)))

DB = "quantopsai_profile_212.db"
OCC = "CVX260717C00180000"
OPEN_ROW = 91
CLOSE_ORDER = "3b5758a0-4b5b-4019-9b0e-ed8e1ba14dbf"
QTY = 4.0
CLOSE_PX = 7.10
OPEN_PX = 1.04
# house sign convention (journal.realized_option_close_pnl): short leg
# realized = (premium_in − close_px) × qty × 100
TRUE_PNL = round((OPEN_PX - CLOSE_PX) * QTY * 100.0, 2)   # −2424.00

REASON_INSERT = (
    "repair 2026-07-24: broker expiry auto-liquidation of the short "
    "180C — BUY 4 @ $7.10 (FILL activity 2026-07-17T19:30:42Z, order "
    "3b5758a0) was never journaled by the pre-07-20 assignment sweep; "
    "cash −$2,840 and realized −$2,424 restored from broker truth "
    "(the acct-56 cash_parity −$2,852 root)"
)
REASON_NULL_PNL = (
    "repair 2026-07-24: cleared fabricated +416 pnl from this "
    "sell-to-open row — pnl belongs on the close row (inserted with "
    "order 3b5758a0); truth is −2,424 realized on this leg"
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    # --- broker verification ------------------------------------------
    from models import build_user_context_from_profile
    api = build_user_context_from_profile(212).get_alpaca_api()
    o = api.get_order(CLOSE_ORDER)
    st, side = str(getattr(o, "status", "")), str(getattr(o, "side", ""))
    fq = float(getattr(o, "filled_qty", 0) or 0)
    fp = float(getattr(o, "filled_avg_price", 0) or 0)
    print(f"broker {CLOSE_ORDER[:8]}: status={st} side={side} "
          f"filled={fq} @ {fp}")
    if st != "filled" or side != "buy" or fq != QTY or abs(fp - CLOSE_PX) > 0.005:
        print("REFUSE: broker does not confirm the liquidation fill")
        return 1

    # --- idempotency + journal shape ----------------------------------
    for pid in (211, 212, 213, 214, 215):
        conn = sqlite3.connect(f"quantopsai_profile_{pid}.db")
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE order_id = ?",
                (CLOSE_ORDER,)).fetchone()[0]
        finally:
            conn.close()
        if n:
            print(f"REFUSE: p{pid} already journals order {CLOSE_ORDER[:8]} "
                  f"({n} row(s)) — nothing to repair")
            return 1
    conn = sqlite3.connect(DB)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, side, qty, price, status, pnl, occ_symbol "
            "FROM trades WHERE id=?", (OPEN_ROW,)).fetchone()
        if (not row or row["side"] != "sell" or float(row["qty"]) != QTY
                or row["occ_symbol"] != OCC or row["status"] != "closed"
                or abs(float(row["price"]) - OPEN_PX) > 0.005):
            print(f"REFUSE: row #{OPEN_ROW} shape changed: "
                  f"{dict(row) if row else None}")
            return 1
        if not args.apply:
            print(f"DRY-RUN: would INSERT buy-to-close {QTY:.0f}x {OCC} "
                  f"@ {CLOSE_PX} (pnl {TRUE_PNL}) and NULL #{OPEN_ROW}'s "
                  f"pnl (currently {row['pnl']}). Re-run with --apply.")
            return 0
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, "
            " fill_price, order_id, occ_symbol, signal_type, strategy, "
            " status, pnl, reason) "
            "VALUES (?, 'CVX', 'buy', ?, ?, ?, ?, ?, 'OPASN', "
            "        'alpaca_activity', 'closed', ?, ?)",
            ("2026-07-17T19:30:42.484091", QTY, CLOSE_PX, CLOSE_PX,
             CLOSE_ORDER, OCC, TRUE_PNL, REASON_INSERT),
        )
        conn.execute(
            "UPDATE trades SET pnl = NULL, "
            "reason = COALESCE(reason || ' | ', '') || ? WHERE id = ?",
            (REASON_NULL_PNL, OPEN_ROW),
        )
        conn.commit()
        new_id = conn.execute(
            "SELECT id FROM trades WHERE order_id=?",
            (CLOSE_ORDER,)).fetchone()[0]
        print(f"APPLIED: inserted close row #{new_id}; "
              f"#{OPEN_ROW} pnl cleared")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
