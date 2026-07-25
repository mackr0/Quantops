"""Repair the 2026-07-24 expiry-weekend books: the mis-captured CVX
assignment, the never-booked worthless expiries, and the dead XOM sell.

What Friday's close left (all broker-verified this session):
  - OPASN CVX260724C00185000 x2 (p212's short leg): Alpaca paper removed
    the option with net_amount=0 — NO cash, NO shares (CVX broker 25 ==
    p215's 25 exactly; no CVX FILL activities). The manual capture run
    then wrote the SAME assignment row into ALL FIVE acct-56 profiles
    (no ownership gate) with side='sell' (wrong for a short leg).
  - OPEXP FCX260724C00070000 qty=-5 (p212's long leg): expired
    worthless; the capture SKIPPED it (negative qty guard).
  - CVX260724C00190000 x2 (p212's long leg): gone at the broker (no
    OPEXP activity posted, but position truth is 0); never booked.
  - p211 #353 (XOM260731P sell 1): broker order expired unfilled at the
    close; row stuck 'pending_fill' (journal reads the put as netted
    while the broker still holds it).

Writes (with --apply):
  1. VOID the five 15:34Z capture rows (status='canceled', signed) —
     kept, not deleted, so the capture's activity-id dedup stays
     engaged and Monday's scheduled run cannot re-write them.
  2. p212 correct bookings (all price 0 per net_amount=0; pnl stamped
     explicitly because recompute_realized_pnl skips price-0 rows):
       - 185C BUY-to-close 2 @0, pnl +274.00 (premium 1.37 x 200 kept)
       - 190C SELL-to-close 2 @0, pnl -136.00 (premium 0.68 x 200 lost)
       - FCX 70C SELL-to-close 5 @0, pnl -60.00 (premium 0.12 x 500)
  3. p211 #353 -> status='expired' (broker-verified terminal-unfilled).

DRY-RUN by default; --apply to write. Idempotent (refuses re-runs).
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir)))

ASN_ID = "20260724000000000::c3108120-f92d-4ef9-9d7b-54bb7691b205"
EXP_ID = "20260724000000000::7818e9d2-9dd3-4eab-9004-6619565da57e"

VOID_REASON = (
    "voided by repair_expiry_weekend_2026_07_25: account-level OPASN "
    "was written into every acct-56 profile (capture had no own-book "
    "ownership gate) and with side='sell' (wrong for a short leg); the "
    "correct booking lives in p212 only. Row kept so the activity-id "
    "dedup stays engaged."
)

BOOKINGS = [
    # (occ, side, qty, pnl, signal, activity_ref, note)
    ("CVX260724C00185000", "buy", 2.0, 274.00, "OPASN", ASN_ID,
     "short leg assigned; Alpaca paper removed it net_amount=0 (no "
     "cash, no shares) — full premium 1.37x200 realized"),
    ("CVX260724C00190000", "sell", 2.0, -136.00, "OPEXP", ASN_ID,
     "long leg gone at broker post-expiry (position truth; no OPEXP "
     "activity posted) — premium 0.68x200 lost"),
    ("FCX260724C00070000", "sell", 5.0, -60.00, "OPEXP", EXP_ID,
     "expired worthless (OPEXP qty=-5 was skipped by the capture's "
     "negative-qty guard) — premium 0.12x500 lost"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    # --- broker verification -------------------------------------------
    from models import build_user_context_from_profile
    api = build_user_context_from_profile(212).get_alpaca_api()
    held = {str(p.symbol).replace(" ", "") for p in api.list_positions()}
    for occ, *_ in BOOKINGS:
        if occ in held:
            print(f"REFUSE: broker still holds {occ} — not expired")
            return 1
    conn = sqlite3.connect("quantopsai_profile_211.db")
    try:
        xom_oid = conn.execute(
            "SELECT order_id FROM trades WHERE id=353").fetchone()[0]
    finally:
        conn.close()
    o = api.get_order(xom_oid)
    if str(o.status) != "expired" or float(o.filled_qty or 0) != 0:
        print(f"REFUSE: XOM order {str(xom_oid)[:8]} is "
              f"{o.status}/{o.filled_qty}, not expired-unfilled")
        return 1
    print("broker verified: 3 OCCs absent; XOM sell expired-unfilled")

    # --- idempotency ---------------------------------------------------
    c212 = sqlite3.connect("quantopsai_profile_212.db")
    try:
        n = c212.execute(
            "SELECT COUNT(*) FROM trades WHERE order_id LIKE '%::repair'"
        ).fetchone()[0]
    finally:
        c212.close()
    if n:
        print("REFUSE: repair rows already present — nothing to do")
        return 1

    if not args.apply:
        print("DRY-RUN: would void 5 capture rows, book 3 closes in "
              "p212 (pnl +274/-136/-60), expire p211 #353. --apply to "
              "write.")
        return 0

    # 1. void the five duplicated capture rows
    for pid in (211, 212, 213, 214, 215):
        conn = sqlite3.connect(f"quantopsai_profile_{pid}.db")
        try:
            cur = conn.execute(
                "UPDATE trades SET status='canceled', "
                "reason = COALESCE(reason || ' | ', '') || ? "
                "WHERE order_id = ? AND strategy='alpaca_activity'",
                (VOID_REASON, ASN_ID))
            conn.commit()
            print(f"p{pid}: voided {cur.rowcount} capture row(s)")
        finally:
            conn.close()

    # 2. correct bookings in p212
    conn = sqlite3.connect("quantopsai_profile_212.db")
    try:
        import re
        for occ, side, qty, pnl, sig, act_id, note in BOOKINGS:
            m = re.search(r"(\d{6}[CP]\d{8})$", occ)
            underlying = occ[:m.start()] if m else occ
            conn.execute(
                "INSERT INTO trades (timestamp, symbol, side, qty, "
                " price, fill_price, order_id, occ_symbol, signal_type, "
                " strategy, status, pnl, reason) "
                "VALUES ('2026-07-24T20:00:00.000001', ?, ?, ?, 0, 0, "
                "        ?, ?, ?, 'alpaca_activity', 'closed', ?, ?)",
                (underlying, side, qty,
                 act_id + "::repair::" + occ, occ, sig, pnl,
                 f"repair_expiry_weekend_2026_07_25: {note} "
                 f"(activity {act_id})"))
        conn.commit()
        print("p212: booked 3 closes (pnl +274.00, -136.00, -60.00)")
    finally:
        conn.close()

    # 3. expire the dead XOM sell
    conn = sqlite3.connect("quantopsai_profile_211.db")
    try:
        conn.execute(
            "UPDATE trades SET status='expired', "
            "reason = COALESCE(reason || ' | ', '') || ? WHERE id=353",
            ("repair_expiry_weekend_2026_07_25: broker order expired "
             "unfilled at Friday close (verified filled_qty=0); the "
             "put is still held — row must not net the position",))
        conn.commit()
        print("p211: #353 -> expired")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
