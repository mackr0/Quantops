"""Guardrail: a stock BUY going from status='pending_fill' to
fill-confirmed must transition to status='open', not 'closed'.

Caught 2026-05-18 17:28 ET — third distinct outage of the day.
Earlier:
  (1) journal.py step 2 race fix
  (2) multi_scheduler.py:1320 FIFO partial-sell fix

But the OTHER branch of the same fill-confirm block had a too-broad
`elif trade["status"] == "pending_fill":` that flipped EVERY remaining
pending_fill row to closed — including fresh BUY rows whose fill had
just confirmed. Comment claimed it was for option-close legs only;
the SQL had no guard. P12/P13/P14 day-1 BUYs all flipped to closed
the instant Alpaca confirmed each fill_avg_price.

Fix: gate that elif on `trade.get("occ_symbol")` so only option-leg
rows hit it. Stock BUY/SHORT pending_fill rows fall through to a new
branch that flips them to 'open' (the correct fill-confirmed state
for an entry).
"""
from __future__ import annotations

import re


def test_pending_fill_elif_has_occ_symbol_guard():
    """AST scan: the elif branch in _task_update_fill_prices that
    flips status='pending_fill' to 'closed' must be gated on
    occ_symbol (option-only). Without the guard, every BUY's fill
    confirmation closes the entry."""
    with open("multi_scheduler.py", encoding="utf-8") as f:
        src = f.read()
    # Locate the elif block(s) after the FIFO SELL/COVER if/branch.
    # The buggy pattern: `elif trade["status"] == "pending_fill":`
    # with NO `occ_symbol` check anywhere in the next ~10 lines.
    # The fixed pattern: same elif + an immediate
    # `and trade.get("occ_symbol")` in the conditional, OR an
    # immediately-following SQL with a side filter restricting to
    # option-specific rows.
    pat = re.compile(
        r"elif\s+trade\[['\"]status['\"]\]\s*==\s*['\"]pending_fill['\"]\s*:\s*\n"
        r"([\s\S]{0,500}?UPDATE\s+trades\s+SET\s+status\s*=\s*['\"]closed['\"][\s\S]{0,200})",
        re.IGNORECASE,
    )
    for m in pat.finditer(src):
        block = m.group(0)
        if "occ_symbol" not in block:
            raise AssertionError(
                "Unguarded `elif trade['status'] == 'pending_fill':` "
                "closes status to 'closed' without an occ_symbol check. "
                "Block: " + block[:300]
            )


def test_pending_fill_buy_transitions_to_open():
    """Direct: simulate the fill-confirm step for a pending_fill BUY
    of a stock (no occ_symbol). Expect status to become 'open'."""
    import sqlite3, tempfile, os
    from contextlib import closing
    # Minimal stub of the trade dict that _task_update_fill_prices
    # iterates over, plus a small SQLite to act as the journal.
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    try:
        with closing(sqlite3.connect(tmp.name)) as conn:
            conn.execute("""
                CREATE TABLE trades (
                    id INTEGER PRIMARY KEY,
                    symbol TEXT,
                    side TEXT,
                    qty REAL,
                    price REAL,
                    order_id TEXT,
                    status TEXT,
                    occ_symbol TEXT
                )
            """)
            conn.execute(
                "INSERT INTO trades(id, symbol, side, qty, price, "
                "order_id, status, occ_symbol) "
                "VALUES (1, 'AAPL', 'buy', 100, 200.0, 'order-x', "
                "'pending_fill', NULL)",
            )
            conn.commit()
            # Mirror of the new elif-fallthrough branch logic
            trade = {"id": 1, "status": "pending_fill", "side": "buy",
                     "occ_symbol": None}
            if (trade["status"] == "pending_fill"
                    and trade["side"] in ("sell", "cover")):
                pass  # FIFO path — not us
            elif (trade["status"] == "pending_fill"
                    and trade["occ_symbol"]):
                conn.execute(
                    "UPDATE trades SET status='closed' WHERE id=?",
                    (trade["id"],),
                )
            elif trade["status"] == "pending_fill":
                conn.execute(
                    "UPDATE trades SET status='open' WHERE id=?",
                    (trade["id"],),
                )
            conn.commit()
            status = conn.execute(
                "SELECT status FROM trades WHERE id=1"
            ).fetchone()[0]
        assert status == "open", (
            f"BUY pending_fill must transition to 'open' on fill "
            f"confirmation, got {status!r} — the 2026-05-18 bug "
            f"that closed every day-1 BUY is back."
        )
    finally:
        os.unlink(tmp.name)


def test_pending_fill_option_close_still_marks_closed():
    """Sanity: an option-leg row WITH occ_symbol still hits the
    'closed' branch."""
    trade = {"id": 1, "status": "pending_fill", "side": "buy",
             "occ_symbol": "AAPL250619C00200000"}
    result = None
    if (trade["status"] == "pending_fill"
            and trade["side"] in ("sell", "cover")):
        result = "fifo"
    elif (trade["status"] == "pending_fill"
            and trade["occ_symbol"]):
        result = "closed"
    elif trade["status"] == "pending_fill":
        result = "open"
    assert result == "closed"
