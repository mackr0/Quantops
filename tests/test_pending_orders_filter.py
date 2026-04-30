"""Pending orders panel must show only THIS profile's orders, not the
shared Alpaca account's full open-order list.

History 2026-04-30: 10 profiles share 3 Alpaca paper accounts. The
dashboard's pending-orders panel called api.list_orders(), which
returns every open order on the shared account — so a profile's
panel showed orders placed by all 6 sibling profiles sharing the
account. Confusing pattern: 'why does Mid Cap show stop orders for
SOFI? It doesn't hold SOFI.' Answer: SOFI is held by another profile
on the same Alpaca account, and that profile placed the stop.

Fix: cross-reference each Alpaca order's id against THIS profile's
trades table. Hide orders whose id isn't in our DB.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _init_db(path):
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            timestamp TEXT, symbol TEXT, side TEXT, qty REAL, price REAL,
            order_id TEXT, signal_type TEXT, strategy TEXT,
            reason TEXT, ai_reasoning TEXT, ai_confidence INTEGER,
            stop_loss REAL, take_profit REAL, status TEXT, pnl REAL,
            decision_price REAL, fill_price REAL, slippage_pct REAL,
            max_favorable_excursion REAL,
            protective_stop_order_id TEXT,
            protective_tp_order_id TEXT,
            protective_trailing_order_id TEXT
        )
    """)
    conn.commit()
    conn.close()


def _seed_owned(db, **ids):
    """Seed a single trades row with the given order_id columns set."""
    cols = ["timestamp", "symbol", "side", "qty", "price", "status"]
    vals = ["2026-04-30", "AAPL", "buy", 100, 150.0, "open"]
    for col, val in ids.items():
        cols.append(col)
        vals.append(val)
    placeholders = ",".join("?" * len(vals))
    conn = sqlite3.connect(db)
    conn.execute(
        f"INSERT INTO trades ({','.join(cols)}) VALUES ({placeholders})",
        vals,
    )
    conn.commit()
    conn.close()


def _make_order(order_id, symbol="AAPL", side="sell", qty=100,
                  order_type="trailing_stop", status="held"):
    o = MagicMock()
    o.id = order_id
    o.symbol = symbol
    o.side = side
    o.qty = qty
    o.order_type = order_type
    o.limit_price = None
    o.status = status
    o.submitted_at = "2026-04-30T17:00:00Z"
    o.time_in_force = "gtc"
    return o


def test_pending_orders_hides_sibling_profile_orders(tmp_path):
    """Alpaca returns 3 orders on the shared account. Only ONE belongs
    to our profile (matched by id in the trades DB). The other two
    are sibling-profile orders and must be hidden."""
    from views import _safe_pending_orders
    db = str(tmp_path / "trades.db")
    _init_db(db)
    _seed_owned(db, protective_trailing_order_id="ours-trail")

    api = MagicMock()
    api.list_orders.return_value = [
        _make_order("ours-trail", symbol="AAPL"),
        _make_order("sibling-stop", symbol="SOFI"),  # different profile
        _make_order("sibling-tp", symbol="TSLA"),    # different profile
    ]
    ctx = MagicMock()
    ctx.db_path = db
    ctx.get_alpaca_api.return_value = api
    ctx.display_name = "Test"
    ctx.segment = "small"

    out = _safe_pending_orders(ctx)
    syms = [o["symbol"] for o in out]
    assert syms == ["AAPL"]


def test_pending_orders_unions_all_id_columns(tmp_path):
    """A profile's owned orders span four columns: entry order_id +
    three protective_*_order_id columns. All four must be in the union."""
    from views import _safe_pending_orders
    db = str(tmp_path / "trades.db")
    _init_db(db)
    _seed_owned(db, order_id="entry-1", protective_stop_order_id="stop-1",
                  protective_tp_order_id="tp-1",
                  protective_trailing_order_id="trail-1")

    api = MagicMock()
    api.list_orders.return_value = [
        _make_order("entry-1"),
        _make_order("stop-1"),
        _make_order("tp-1"),
        _make_order("trail-1"),
        _make_order("stranger"),
    ]
    ctx = MagicMock()
    ctx.db_path = db
    ctx.get_alpaca_api.return_value = api

    out = _safe_pending_orders(ctx)
    ids = sorted([o["symbol"] for o in out])  # all AAPL by default
    assert len(out) == 4
    # The stranger is filtered out
    api_ids_returned = [o.id for o in api.list_orders.return_value]
    assert "stranger" in api_ids_returned  # confirm test setup
    # But not in the visible output (no symbol marker tells us this;
    # just count: 5 returned by Alpaca, 4 visible)


def test_pending_orders_falls_open_when_db_unreadable(tmp_path):
    """If the trades DB can't be read, show ALL orders — better to
    show extras than to hide everything and confuse the user."""
    from views import _safe_pending_orders
    db = "/nonexistent/path/that/will/error.db"

    api = MagicMock()
    api.list_orders.return_value = [
        _make_order("any-id-1"),
        _make_order("any-id-2"),
    ]
    ctx = MagicMock()
    ctx.db_path = db
    ctx.get_alpaca_api.return_value = api

    out = _safe_pending_orders(ctx)
    # Fail-open: when we can't establish ownership, show everything
    # rather than hide everything.
    assert len(out) == 2


def test_pending_orders_handles_missing_protective_columns(tmp_path):
    """Older trades DBs (pre-INTRADAY_STOPS) don't have the
    protective_*_order_id columns. The query for those must be
    skipped silently, falling back to entry order_id + nothing else."""
    from views import _safe_pending_orders
    db = str(tmp_path / "old.db")
    # Old schema — no protective_* columns
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            timestamp TEXT, symbol TEXT, side TEXT, qty REAL, price REAL,
            order_id TEXT, status TEXT
        )
    """)
    conn.execute(
        "INSERT INTO trades (timestamp, symbol, side, qty, price, order_id, status) "
        "VALUES (?, 'AAPL', 'buy', 100, 150.0, 'old-entry', 'open')",
        ("2026-04-30",),
    )
    conn.commit()
    conn.close()

    api = MagicMock()
    api.list_orders.return_value = [
        _make_order("old-entry"),
        _make_order("not-ours"),
    ]
    ctx = MagicMock()
    ctx.db_path = db
    ctx.get_alpaca_api.return_value = api

    out = _safe_pending_orders(ctx)
    # The old-entry id is matched; the not-ours is filtered.
    # Test should not crash on missing columns.
    assert len(out) == 1
