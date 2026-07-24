"""Repair the NFLX 123-share cross-profile oversell (07-22 storm remnant).

What physically happened (broker-verified this session): p214's trailing
stop (order 86205430) PARTIALLY filled 123 of 269 on 07-09; the partial
went unconfirmed for days (429-era healer starvation), so on 07-17 its
journal still believed 269 held and its stop-loss (order 14bc4c37) sold
269 — 123 more than p214 owned. On the shared account those 123 shares
were delivered out of p213's inventory: p213's 270-lot is physically
backed by 147 (= today's broker position). Both sell orders are
genuinely p214's own; nothing is mis-attributed. Effect on the books:
p214 nets −123 with no 'short' rows (the SCAN data-integrity warning),
p213 claims 270 it can't fully realize.

Repair: an INTERNAL INVENTORY TRANSFER at the actual excess-exit price
($66.57, order 14bc4c37's broker avg fill): p213 SELLS 123 → p214 BUYS
123, same timestamp, synthetic non-broker order ids (XFER- prefix, the
REPAIR-O convention), both signed. Account-level effects: position
aggregate unchanged (−123/+123 cancel), cash aggregate unchanged (equal
qty × same price both directions) — so the account-vs-broker audits
stay at zero by construction; per-profile books become economically
true (p213 long 147 = its real backing; p214 net flat). pnl left NULL —
recompute_realized_pnl stamps it from FIFO (single source of the sign
convention).

Refuses unless the current shapes still hold (p213 #53 open buy 270;
p214 NFLX net exactly −123 from its filled rows; broker order 14bc4c37
filled sell 269 @66.57) and no XFER row already exists (idempotent).
DRY-RUN by default; --apply to write.
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir)))

QTY = 123.0
PX = 66.57                     # broker avg fill of the excess exit
TS = "2026-07-17T13:31:37.000001"   # the moment of the oversell exit
OID_213 = "XFER-NFLX-213to214-20260724-sell"
OID_214 = "XFER-NFLX-213to214-20260724-buy"
EXIT_ORDER_PREFIX = "14bc4c37"

R213 = (
    "repair 2026-07-24: internal inventory transfer — 123 of this "
    "profile's NFLX shares were physically delivered into p214's "
    "oversized 07-17 stop-loss (order 14bc4c37, filled 269 @66.57; "
    "p214's own inventory was 146 after its unconfirmed 07-09 partial "
    "trailing-stop fill). Booked as a sell @66.57 (the actual exit "
    "price) so this book's long = its real broker backing (147)."
)
R214 = (
    "repair 2026-07-24: internal inventory transfer — this profile's "
    "07-17 stop-loss (order 14bc4c37) sold 269 while its own inventory "
    "was 146 (the 07-09 partial trailing-stop fill was unconfirmed for "
    "8 days); the excess 123 shares came from p213's inventory. Booked "
    "as a buy @66.57 (the actual exit price) so this book nets flat "
    "instead of showing a phantom short."
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    # --- idempotency ---------------------------------------------------
    for pid in (213, 214):
        conn = sqlite3.connect(f"quantopsai_profile_{pid}.db")
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE order_id LIKE 'XFER-NFLX-%'"
            ).fetchone()[0]
        finally:
            conn.close()
        if n:
            print(f"REFUSE: p{pid} already carries an XFER-NFLX row")
            return 1

    # --- journal shape preconditions ------------------------------------
    c213 = sqlite3.connect("quantopsai_profile_213.db")
    try:
        c213.row_factory = sqlite3.Row
        lot = c213.execute(
            "SELECT id, side, qty, status FROM trades WHERE id=53"
        ).fetchone()
        if (not lot or lot["side"] != "buy" or float(lot["qty"]) != 270.0
                or (lot["status"] or "open") != "open"):
            print(f"REFUSE: p213 #53 shape changed: "
                  f"{dict(lot) if lot else None}")
            return 1
    finally:
        c213.close()
    c214 = sqlite3.connect("quantopsai_profile_214.db")
    try:
        net = c214.execute(
            "SELECT COALESCE(SUM(CASE WHEN side IN ('buy','cover') THEN qty "
            "WHEN side IN ('sell','short') THEN -qty END), 0) FROM trades "
            "WHERE symbol='NFLX' AND occ_symbol IS NULL "
            "AND fill_price IS NOT NULL "
            "AND COALESCE(status,'open') NOT IN ('canceled','expired')"
        ).fetchone()[0]
        if float(net) != -QTY:
            print(f"REFUSE: p214 NFLX filled net is {net}, expected -123")
            return 1
    finally:
        c214.close()

    # --- broker verification -------------------------------------------
    from models import build_user_context_from_profile
    c214db = sqlite3.connect("quantopsai_profile_214.db")
    try:
        full_oid = c214db.execute(
            "SELECT order_id FROM trades WHERE id=230").fetchone()[0]
    finally:
        c214db.close()
    if not str(full_oid).startswith(EXIT_ORDER_PREFIX):
        print(f"REFUSE: p214 #230 order id changed: {full_oid}")
        return 1
    api = build_user_context_from_profile(214).get_alpaca_api()
    o = api.get_order(full_oid)
    st = str(getattr(o, "status", ""))
    fq = float(getattr(o, "filled_qty", 0) or 0)
    fp = float(getattr(o, "filled_avg_price", 0) or 0)
    print(f"broker {EXIT_ORDER_PREFIX}: status={st} filled={fq} @ {fp}")
    if st != "filled" or fq != 269.0 or abs(fp - PX) > 0.005:
        print("REFUSE: broker does not confirm the oversized exit")
        return 1

    if not args.apply:
        print(f"DRY-RUN: would book p213 SELL {QTY:.0f} NFLX @ {PX} and "
              f"p214 BUY {QTY:.0f} NFLX @ {PX} (both closed, signed, "
              f"pnl NULL for recompute). Re-run with --apply.")
        return 0

    conn = sqlite3.connect("quantopsai_profile_213.db")
    try:
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, "
            " fill_price, order_id, signal_type, strategy, status, reason) "
            "VALUES (?, 'NFLX', 'sell', ?, ?, ?, ?, "
            "        'INVENTORY_TRANSFER', 'repair', 'closed', ?)",
            (TS, QTY, PX, PX, OID_213, R213),
        )
        conn.commit()
    finally:
        conn.close()
    conn = sqlite3.connect("quantopsai_profile_214.db")
    try:
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, "
            " fill_price, order_id, signal_type, strategy, status, reason) "
            "VALUES (?, 'NFLX', 'buy', ?, ?, ?, ?, "
            "        'INVENTORY_TRANSFER', 'repair', 'closed', ?)",
            (TS, QTY, PX, PX, OID_214, R214),
        )
        conn.commit()
    finally:
        conn.close()
    print("APPLIED: p213 sell 123 + p214 buy 123 @66.57 (signed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
