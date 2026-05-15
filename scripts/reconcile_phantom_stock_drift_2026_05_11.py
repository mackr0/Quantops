"""One-shot journal reconcile for the 2026-05-11 phantom-stock-sell
fallout. Brings each affected profile's journal back into alignment
with the actual broker holdings.

The bug class (recap).
On 2026-05-11 the system fired 28 stock SELL orders against
multileg leg positions. Each order was journaled with a phantom
price ($0.15-$1.13, the option premium) but Alpaca actually
executed at market price. Net effect: real (paper) shares of
AAPL/KO were sold, broker positions adjusted, but the journal
still shows the original BUY rows as `status='closed'` (so FIFO
shows zero open) AND the polluted SELLs as `status='closed'`.
Result: the dashboard reads zero for these positions but the
broker holds 8 AAPL + 17 KO.

This script:

  1. For each affected (profile, symbol), re-opens the original
     stock BUY row (status='closed' → 'open'). Now FIFO consumes
     the polluted SELLs against the original lot.

  2. For shared-account profiles where one profile's polluted
     SELLs sold from another profile's shared bucket, ALSO marks
     the cross-profile-affecting SELLs status='canceled' so
     FIFO doesn't double-count.

  3. For the original BUYer, adds a synthetic SELL row that
     captures the cross-profile share adjustment so the FIFO net
     matches the broker exactly.

  4. Verifies post-state per profile.

Specific adjustments (computed from on-disk state 2026-05-15):

  pid 4 (Large Cap, acct 3):
    AAPL: re-open row 25 (BUY 13). FIFO 13 − 5 polluted = 8 ✓
    KO:   re-open row 113 (BUY 53). Plus synthetic SELL 26 for
          pid 11's cross-profile drain. FIFO 53 − 10 − 26 = 17 ✓

  pid 11 (Large Cap Limit Orders, acct 3 — shares with pid 4):
    KO:   the 13 polluted SELLs (26 shares) are also marked
          status='canceled' so pid 11's FIFO shows 0 KO (correct
          — pid 11 never owned KO; the broker had it via pid 4).
          The polluted tag stays for analytics; canceled keeps
          FIFO honest.

Idempotent: re-running is safe (the WHERE clauses skip already-
adjusted rows).
"""
from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing

DB_DIR = "/opt/quantopsai"

# Adjustments computed from broker state 2026-05-15.
# Each entry is one operation against one profile's journal.
RECONCILE_OPS = [
    # pid 4 AAPL: re-open the original BUY so FIFO consumes the
    # 5 polluted SELLs against it; net 8 matches broker.
    {
        "kind": "reopen_buy",
        "profile_id": 4,
        "trade_id": 25,
        "symbol": "AAPL",
        "rationale": (
            "Re-open original BUY 13 — 5 polluted SELLs already "
            "tagged; FIFO consumes them against this lot, net 8 "
            "matches broker (verified 2026-05-15)."
        ),
    },
    # pid 4 KO: re-open the original BUY.
    {
        "kind": "reopen_buy",
        "profile_id": 4,
        "trade_id": 113,
        "symbol": "KO",
        "rationale": (
            "Re-open original BUY 53 — 10 polluted SELLs on pid 4 "
            "consume against this lot. Cross-profile adjustment "
            "below brings FIFO net to 17, matching broker."
        ),
    },
    # pid 4 KO cross-profile sync: pid 11's polluted SELLs sold
    # from pid 4's shared-account bucket. Add a synthetic SELL 26
    # to pid 4 to capture that drain.
    {
        "kind": "synthetic_sell",
        "profile_id": 4,
        "symbol": "KO",
        "qty": 26,
        "price": 78.61,  # broker avg as of 2026-05-15
        "rationale": (
            "Cross-profile shared-account adjustment: pid 11 fired "
            "13 polluted SELLs of 2 KO each from acct 3's shared "
            "bucket. The shares were originally bought by pid 4. "
            "Capturing as a synthetic SELL on pid 4's journal so "
            "FIFO net matches the broker (53 − 10 − 26 = 17)."
        ),
    },
    # pid 11 KO: cancel the 13 polluted SELLs so pid 11's FIFO
    # shows 0 KO (correct — pid 11 never had a KO BUY).
    {
        "kind": "cancel_polluted_sells",
        "profile_id": 11,
        "symbol": "KO",
        "rationale": (
            "Pid 11 never bought KO stock. The 13 polluted SELLs "
            "consumed from acct 3's shared bucket which pid 4 "
            "actually owns. Cancel these on pid 11 so FIFO shows "
            "0 KO; the cross-profile adjustment on pid 4 "
            "captures the shared-account drain."
        ),
    },
]


def _reopen_buy(op: dict) -> str:
    db = f"{DB_DIR}/quantopsai_profile_{op['profile_id']}.db"
    with closing(sqlite3.connect(db)) as conn:
        cur = conn.execute(
            "UPDATE trades SET status='open', "
            "reason=COALESCE(reason || ' | ', '') || ? "
            "WHERE id=? AND status='closed'",
            (
                f"reconcile-2026-05-15: {op['rationale']}",
                op["trade_id"],
            ),
        )
        conn.commit()
        if cur.rowcount > 0:
            return f"  pid {op['profile_id']} {op['symbol']}: re-opened row {op['trade_id']}"
        return f"  pid {op['profile_id']} {op['symbol']}: row {op['trade_id']} already open or missing — skipped"


def _synthetic_sell(op: dict) -> str:
    db = f"{DB_DIR}/quantopsai_profile_{op['profile_id']}.db"
    with closing(sqlite3.connect(db)) as conn:
        # Idempotency: skip if a matching synthetic SELL already exists
        existing = conn.execute(
            "SELECT id FROM trades WHERE symbol=? AND side='sell' "
            "AND signal_type='reconcile_xprof' AND qty=?",
            (op["symbol"], op["qty"]),
        ).fetchone()
        if existing:
            return f"  pid {op['profile_id']} {op['symbol']}: synthetic SELL {op['qty']} already present (id={existing[0]}) — skipped"
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, "
            "signal_type, status, reason, data_quality) "
            "VALUES (datetime('now'), ?, 'sell', ?, ?, "
            "'reconcile_xprof', 'closed', ?, 'reconcile_adjustment')",
            (
                op["symbol"], op["qty"], op["price"],
                f"reconcile-2026-05-15: {op['rationale']}",
            ),
        )
        conn.commit()
        return f"  pid {op['profile_id']} {op['symbol']}: inserted synthetic SELL {op['qty']} @ ${op['price']}"


def _cancel_polluted_sells(op: dict) -> str:
    db = f"{DB_DIR}/quantopsai_profile_{op['profile_id']}.db"
    with closing(sqlite3.connect(db)) as conn:
        cur = conn.execute(
            "UPDATE trades SET status='canceled', "
            "reason=COALESCE(reason || ' | ', '') || ? "
            "WHERE symbol=? AND side='sell' AND occ_symbol IS NULL "
            "AND data_quality='polluted' AND status='closed'",
            (
                f"reconcile-2026-05-15: {op['rationale']}",
                op["symbol"],
            ),
        )
        conn.commit()
        return f"  pid {op['profile_id']} {op['symbol']}: canceled {cur.rowcount} polluted SELL row(s)"


HANDLERS = {
    "reopen_buy": _reopen_buy,
    "synthetic_sell": _synthetic_sell,
    "cancel_polluted_sells": _cancel_polluted_sells,
}


def main():
    print("=" * 70)
    print("Reconcile journal to broker — 2026-05-11 phantom-stock fallout")
    print("=" * 70)
    print()

    print("Operations to apply:")
    for op in RECONCILE_OPS:
        print(f"  - pid {op['profile_id']} {op['symbol']}: {op['kind']}")
    print()
    print("Applying:")
    for op in RECONCILE_OPS:
        handler = HANDLERS[op["kind"]]
        print(handler(op))
    print()

    # Verify by computing FIFO net per affected profile/symbol.
    print("=" * 70)
    print("Post-state verification:")
    print("=" * 70)
    for pid, sym in [(4, "AAPL"), (4, "KO"), (11, "KO")]:
        db = f"{DB_DIR}/quantopsai_profile_{pid}.db"
        if not os.path.exists(db):
            continue
        with closing(sqlite3.connect(db)) as conn:
            rows = conn.execute(
                "SELECT side, qty FROM trades "
                "WHERE symbol=? AND occ_symbol IS NULL "
                "AND COALESCE(status, 'open') != 'canceled'",
                (sym,),
            ).fetchall()
        net = sum(
            float(qty) if side == "buy"
            else -float(qty) if side in ("sell", "cover")
            else 0
            for side, qty in rows
        )
        print(f"  pid {pid} {sym}: journal FIFO net = {net:.0f} share(s)")
    print()
    print("Expected (broker on acct 3, queried 2026-05-15):")
    print("  AAPL: 8 shares")
    print("  KO:   17 shares (shared between pid 4 and pid 11; "
          "all attributed to pid 4)")


if __name__ == "__main__":
    sys.exit(main())
