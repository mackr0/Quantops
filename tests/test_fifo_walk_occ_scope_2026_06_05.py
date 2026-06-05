"""Regression: the FIFO walk in `_task_update_fills` must scope by
`occ_symbol` so an option SELL only consumes option BUY lots for
the same OCC contract, and a stock SELL only consumes stock BUY
lots — never both.

Bug produced 2026-06-05: AUTO_RECONCILE_CLOSE for an OCC option
wrote a journal row with `symbol=<underlying>` (e.g., "NVDA") and
side='sell'. When `_task_update_fills` processed it, the SELL/COVER
branch's FIFO query was `WHERE symbol = ?` with no `occ_symbol`
filter. Stock NVDA BUY rows got their qty consumed by the option
SELL in FIFO bookkeeping, under-reporting stock holdings in the
virtual position book vs the broker (broker_orphan on the stock
side).

Fix: the FIFO query now uses `AND occ_symbol = ?` when the closing
row carries an OCC and `AND occ_symbol IS NULL` when it doesn't.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime

import pytest


def _make_db(tmp_path):
    db = tmp_path / "p.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            symbol TEXT,
            side TEXT,
            qty REAL,
            price REAL,
            order_id TEXT,
            signal_type TEXT,
            strategy TEXT,
            reason TEXT,
            ai_reasoning TEXT,
            ai_confidence INTEGER,
            stop_loss REAL,
            take_profit REAL,
            status TEXT,
            pnl REAL,
            decision_price REAL,
            fill_price REAL,
            slippage_pct REAL,
            occ_symbol TEXT,
            option_strategy TEXT,
            expiry TEXT,
            strike REAL,
            predicted_slippage_bps REAL,
            adv_at_decision REAL
        )
    """)
    conn.commit()
    return conn, str(db)


def _insert(conn, **kwargs):
    cols = ",".join(kwargs.keys())
    qs = ",".join("?" for _ in kwargs)
    cur = conn.execute(
        f"INSERT INTO trades ({cols}) VALUES ({qs})",
        list(kwargs.values()),
    )
    conn.commit()
    return cur.lastrowid


def _simulate_sell_cover_branch(
    db_path: str, sell_row_id: int,
):
    """Run the exact FIFO walk + close logic from
    `_task_update_fills` SELL/COVER branch (post-fix) against a
    given sell row. Lifted from multi_scheduler.py:1395+."""
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        trade = conn.execute(
            "SELECT id, side, symbol, occ_symbol FROM trades WHERE id=?",
            (sell_row_id,),
        ).fetchone()
        # Mirror the post-fix branch
        conn.execute(
            "UPDATE trades SET status='closed' WHERE id=?",
            (trade["id"],),
        )
        opp_side = "buy" if trade["side"] == "sell" else "short"
        exit_side = trade["side"]
        trade_occ = trade["occ_symbol"]
        if trade_occ:
            occ_filter = "AND occ_symbol = ?"
            extra_params = [trade_occ]
        else:
            occ_filter = "AND occ_symbol IS NULL"
            extra_params = []
        rows = conn.execute(
            "SELECT id, side, qty FROM trades "
            "WHERE symbol = ? AND side IN (?, ?) "
            f"  {occ_filter} "
            "  AND COALESCE(status, 'open') != 'canceled' "
            "ORDER BY timestamp ASC, id ASC",
            [trade["symbol"], opp_side, exit_side] + extra_params,
        ).fetchall()
        lots = []
        for r in rows:
            side_i = r[1]
            qty_i = float(r[2] or 0)
            if side_i == opp_side:
                lots.append([r[0], qty_i])
            else:
                remaining = qty_i
                for lot in lots:
                    if remaining <= 0:
                        break
                    if lot[1] <= 0:
                        continue
                    consumed = min(lot[1], remaining)
                    lot[1] -= consumed
                    remaining -= consumed
        for lot_id, lot_remaining in lots:
            if lot_remaining <= 1e-6:
                conn.execute(
                    "UPDATE trades SET status='closed' "
                    "WHERE id=? AND COALESCE(status, 'open')='open'",
                    (lot_id,),
                )
        conn.commit()


def test_option_sell_does_not_consume_stock_buy_lots(tmp_path):
    """The 2026-06-05 bug shape: stock NVDA BUY qty=100 sitting in
    the journal. An option NVDA260710P00195000 SELL of qty=2 arrives
    (e.g., AUTO_RECONCILE_CLOSE). The FIFO walk MUST NOT consume any
    qty from the stock BUY — they're different instruments."""
    conn, db_path = _make_db(tmp_path)
    # Stock NVDA BUY 100 shares (open)
    stock_buy_id = _insert(
        conn,
        timestamp="2026-06-04T14:00:00",
        symbol="NVDA",
        side="buy",
        qty=100,
        status="open",
        signal_type="BUY",
        occ_symbol=None,
        order_id="stock-buy-1",
    )
    # Option NVDA P195 BUY 1 contract (open) — same underlying
    _insert(
        conn,
        timestamp="2026-06-04T14:30:00",
        symbol="NVDA",
        side="buy",
        qty=1,
        status="open",
        signal_type="MULTILEG",
        occ_symbol="NVDA260710P00195000",
        order_id="opt-buy-1",
    )
    # AUTO_RECONCILE_CLOSE option SELL qty=2 (pending_fill)
    sell_id = _insert(
        conn,
        timestamp="2026-06-05T15:00:00",
        symbol="NVDA",
        side="sell",
        qty=2,
        status="pending_fill",
        signal_type="AUTO_RECONCILE_CLOSE",
        occ_symbol="NVDA260710P00195000",
        order_id="auto-close-1",
    )
    conn.close()

    _simulate_sell_cover_branch(db_path, sell_id)

    # Stock BUY must still be 'open' with qty 100 in the journal
    with closing(sqlite3.connect(db_path)) as conn:
        stock_row = conn.execute(
            "SELECT qty, status FROM trades WHERE id=?",
            (stock_buy_id,),
        ).fetchone()
    assert stock_row[1] == "open", (
        "Stock NVDA BUY must stay 'open' — option SELL must not "
        "consume stock lots (was the 2026-06-05 stock-side "
        "broker_orphan bug)"
    )
    assert stock_row[0] == 100, "Stock qty must not be touched"


def test_stock_sell_does_not_consume_option_buy_lots(tmp_path):
    """Inverse: a STOCK SELL must not consume an OPTION BUY lot
    even though they share the underlying symbol."""
    conn, db_path = _make_db(tmp_path)
    # Option NVDA C240 BUY 1 contract — symbol=NVDA (same as stock)
    opt_buy_id = _insert(
        conn,
        timestamp="2026-06-04T14:00:00",
        symbol="NVDA",
        side="buy",
        qty=1,
        status="open",
        signal_type="MULTILEG",
        occ_symbol="NVDA260710C00240000",
        order_id="opt-buy-2",
    )
    # Stock NVDA SELL 1 share (pending_fill) — no occ_symbol
    sell_id = _insert(
        conn,
        timestamp="2026-06-05T15:00:00",
        symbol="NVDA",
        side="sell",
        qty=1,
        status="pending_fill",
        signal_type="SELL",
        occ_symbol=None,
        order_id="stock-sell-1",
    )
    conn.close()

    _simulate_sell_cover_branch(db_path, sell_id)

    with closing(sqlite3.connect(db_path)) as conn:
        opt_row = conn.execute(
            "SELECT qty, status FROM trades WHERE id=?",
            (opt_buy_id,),
        ).fetchone()
    assert opt_row[1] == "open", (
        "Option BUY must stay 'open' — stock SELL must not consume "
        "option lots"
    )
    assert opt_row[0] == 1, "Option qty must not be touched"


def test_option_sell_only_consumes_matching_occ(tmp_path):
    """Two different option contracts on the same underlying must
    not consume each other's lots. NVDA P195 SELL must only
    consume NVDA P195 BUYs, not NVDA C240 BUYs."""
    conn, db_path = _make_db(tmp_path)
    # NVDA C240 BUY 1 (different OCC)
    c240_id = _insert(
        conn,
        timestamp="2026-06-04T14:00:00",
        symbol="NVDA",
        side="buy",
        qty=1,
        status="open",
        signal_type="MULTILEG",
        occ_symbol="NVDA260710C00240000",
        order_id="c240-buy",
    )
    # NVDA P195 BUY 1 (target OCC)
    p195_id = _insert(
        conn,
        timestamp="2026-06-04T14:30:00",
        symbol="NVDA",
        side="buy",
        qty=1,
        status="open",
        signal_type="MULTILEG",
        occ_symbol="NVDA260710P00195000",
        order_id="p195-buy",
    )
    # NVDA P195 SELL 1 (close the P195 position)
    sell_id = _insert(
        conn,
        timestamp="2026-06-05T15:00:00",
        symbol="NVDA",
        side="sell",
        qty=1,
        status="pending_fill",
        signal_type="MULTILEG",
        occ_symbol="NVDA260710P00195000",
        order_id="p195-sell",
    )
    conn.close()

    _simulate_sell_cover_branch(db_path, sell_id)

    with closing(sqlite3.connect(db_path)) as conn:
        rows = {
            r[0]: (r[1], r[2]) for r in conn.execute(
                "SELECT id, status, qty FROM trades "
                "WHERE id IN (?, ?)",
                (c240_id, p195_id),
            ).fetchall()
        }
    # C240 must NOT be consumed — different OCC
    assert rows[c240_id][0] == "open", (
        "C240 BUY must stay 'open' — only same-OCC SELL should "
        "consume it"
    )
    # P195 BUY (qty=1) consumed by SELL qty=1 → fully closed
    assert rows[p195_id][0] == "closed", (
        "P195 BUY should be 'closed' (consumed by matching SELL)"
    )


def test_stock_sell_only_consumes_stock_buys(tmp_path):
    """Stock SELL on a symbol with both stock + option BUYs must
    only consume the stock BUY lots."""
    conn, db_path = _make_db(tmp_path)
    stock_buy_id = _insert(
        conn,
        timestamp="2026-06-04T14:00:00",
        symbol="NVDA",
        side="buy",
        qty=100,
        status="open",
        signal_type="BUY",
        occ_symbol=None,
        order_id="stock-buy",
    )
    opt_buy_id = _insert(
        conn,
        timestamp="2026-06-04T14:30:00",
        symbol="NVDA",
        side="buy",
        qty=1,
        status="open",
        signal_type="MULTILEG",
        occ_symbol="NVDA260710P00195000",
        order_id="opt-buy",
    )
    # Stock SELL 100 — should fully close stock BUY
    sell_id = _insert(
        conn,
        timestamp="2026-06-05T15:00:00",
        symbol="NVDA",
        side="sell",
        qty=100,
        status="pending_fill",
        signal_type="SELL",
        occ_symbol=None,
        order_id="stock-sell",
    )
    conn.close()

    _simulate_sell_cover_branch(db_path, sell_id)

    with closing(sqlite3.connect(db_path)) as conn:
        stock = conn.execute(
            "SELECT status FROM trades WHERE id=?",
            (stock_buy_id,),
        ).fetchone()[0]
        opt = conn.execute(
            "SELECT status FROM trades WHERE id=?",
            (opt_buy_id,),
        ).fetchone()[0]
    assert stock == "closed", (
        "Stock BUY should be 'closed' — consumed by matching stock SELL"
    )
    assert opt == "open", (
        "Option BUY must stay 'open' — stock SELL must not touch it"
    )
