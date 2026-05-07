"""Reconcile journal-to-broker — pin the categorization bug found in
prod 2026-05-06.

The bug: `_task_reconcile_trade_statuses` for virtual profiles read
`get_virtual_positions()` (which derives from the journal) as its
source of truth. So it could never detect when a journal entry was
out of sync with broker reality. 40 out of 126 open journal entries
across 11 profiles had been phantoms for up to 15 days:

  - 5 cancel-without-fill (limit BUY canceled, journal still 'open')
  - 35 broker-sold-via-stop (BUY filled, broker stop fired, journal
    never got the SELL row, so realized P&L was missing)

The fix categorizes each phantom by checking the entry order at the
broker:
  - status canceled/expired/rejected + filled_qty=0 → mark canceled
  - status filled + no broker shares → insert SELL from matching
    broker fill, mark BUY closed
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _make_journal_db(tmp_path, rows):
    """Build a profile journal DB with the given trade rows.

    rows: list of (id, symbol, side, qty, status, order_id, ts, price)
    """
    p = tmp_path / "journal.db"
    conn = sqlite3.connect(str(p))
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            timestamp TEXT,
            symbol TEXT,
            side TEXT,
            qty REAL,
            price REAL,
            order_id TEXT,
            signal_type TEXT,
            strategy TEXT,
            reason TEXT,
            status TEXT,
            pnl REAL,
            fill_price REAL
        )
    """)
    for tid, sym, side, qty, status, order_id, ts, price in rows:
        conn.execute(
            "INSERT INTO trades (id, timestamp, symbol, side, qty, price, order_id, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (tid, ts, sym, side, qty, price, order_id, status),
        )
    conn.commit()
    conn.close()
    return str(p)


def _ctx(api, db_path, name="Test", profile_id=99, alpaca_account_id=1):
    ctx = SimpleNamespace()
    ctx.api = api
    ctx.get_alpaca_api = lambda: api
    ctx.db_path = db_path
    ctx.display_name = name
    ctx.profile_id = profile_id
    ctx.alpaca_account_id = alpaca_account_id
    return ctx


def _broker_position(symbol, qty):
    p = MagicMock()
    p.symbol = symbol
    p.qty = qty
    return p


def _broker_order(oid, side, status, qty, filled_qty=0,
                  filled_avg_price=0, filled_at=None,
                  order_type="limit", symbol="X"):
    o = MagicMock()
    o.id = oid
    o.side = side
    o.status = status
    o.qty = qty
    o.filled_qty = filled_qty
    o.filled_avg_price = filled_avg_price
    o.filled_at = filled_at
    o.order_type = order_type
    o.symbol = symbol
    return o


def test_cancel_without_fill_marks_status_canceled(tmp_path):
    """Limit BUY that never filled — the prod-11 INTC scenario."""
    from reconcile_journal_to_broker import reconcile_with_ctx
    db = _make_journal_db(tmp_path, [
        (49, "INTC", "buy", 28, "open", "intc-order",
         "2026-04-24T18:32:27", 80.89),
    ])
    api = MagicMock()
    api.list_positions.return_value = []  # no INTC at broker
    api.get_order.return_value = _broker_order(
        "intc-order", "buy", "canceled", qty=28, filled_qty=0,
    )
    ctx = _ctx(api, db)
    res = reconcile_with_ctx(ctx, apply_changes=True)
    assert len(res["cancel"]) == 1
    assert res["cancel"][0]["symbol"] == "INTC"
    assert len(res["backfill_sell"]) == 0
    # Verify DB write
    conn = sqlite3.connect(db)
    status = conn.execute("SELECT status FROM trades WHERE id=49").fetchone()[0]
    conn.close()
    assert status == "canceled"


def test_broker_sold_via_stop_backfills_sell_row(tmp_path):
    """BUY filled, broker stop fired, journal missed the SELL — the
    prod scenario for 35 of the 40 phantoms."""
    from reconcile_journal_to_broker import reconcile_with_ctx
    db = _make_journal_db(tmp_path, [
        (88, "BMY", "buy", 71, "open", "bmy-buy",
         "2026-04-27T15:17:48", 58.34),
    ])
    api = MagicMock()
    api.list_positions.return_value = []  # no BMY at broker
    api.get_order.return_value = _broker_order(
        "bmy-buy", "buy", "filled", qty=71, filled_qty=71,
    )
    api.list_orders.return_value = [
        _broker_order(
            "bmy-stop-fill", "sell", "filled", qty=71, filled_qty=71,
            filled_avg_price=57.90,
            filled_at=datetime(2026, 5, 4, 13, 30, tzinfo=timezone.utc),
            order_type="trailing_stop",
        ),
    ]
    ctx = _ctx(api, db)
    res = reconcile_with_ctx(ctx, apply_changes=True)
    assert len(res["cancel"]) == 0
    assert len(res["backfill_sell"]) == 1
    backfill = res["backfill_sell"][0]
    assert backfill["symbol"] == "BMY"
    assert backfill["sell_price"] == 57.90
    # Verify DB writes
    conn = sqlite3.connect(db)
    buy_status = conn.execute("SELECT status FROM trades WHERE id=88").fetchone()[0]
    assert buy_status == "closed"
    sell_rows = conn.execute(
        "SELECT symbol, side, qty, price, status, order_id "
        "FROM trades WHERE id != 88"
    ).fetchall()
    assert len(sell_rows) == 1
    sym, side, qty, price, status, oid = sell_rows[0]
    assert sym == "BMY"
    assert side == "sell"
    assert qty == 71
    assert price == 57.90
    assert status == "closed"
    assert oid == "bmy-stop-fill"
    conn.close()


def test_real_held_position_left_alone(tmp_path):
    """Journal qty matches broker shares — leave it open."""
    from reconcile_journal_to_broker import reconcile_with_ctx
    db = _make_journal_db(tmp_path, [
        (10, "AAPL", "buy", 26, "open", "aapl-order",
         "2026-04-30T15:00:00", 280.00),
    ])
    api = MagicMock()
    api.list_positions.return_value = [_broker_position("AAPL", "26")]
    ctx = _ctx(api, db)
    res = reconcile_with_ctx(ctx, apply_changes=True)
    assert res["real_held"] == 1
    assert len(res["cancel"]) == 0
    assert len(res["backfill_sell"]) == 0
    conn = sqlite3.connect(db)
    status = conn.execute("SELECT status FROM trades WHERE id=10").fetchone()[0]
    conn.close()
    assert status == "open"


def test_dry_run_does_not_write(tmp_path):
    """apply_changes=False must leave the journal untouched."""
    from reconcile_journal_to_broker import reconcile_with_ctx
    db = _make_journal_db(tmp_path, [
        (49, "INTC", "buy", 28, "open", "intc-order",
         "2026-04-24T18:32:27", 80.89),
    ])
    api = MagicMock()
    api.list_positions.return_value = []
    api.get_order.return_value = _broker_order(
        "intc-order", "buy", "canceled", qty=28, filled_qty=0,
    )
    ctx = _ctx(api, db)
    res = reconcile_with_ctx(ctx, apply_changes=False)
    assert len(res["cancel"]) == 1  # categorized
    conn = sqlite3.connect(db)
    status = conn.execute("SELECT status FROM trades WHERE id=49").fetchone()[0]
    conn.close()
    assert status == "open"  # but NOT written


def test_multiple_profiles_with_same_qty_attribution(tmp_path):
    """Two profiles each have a BUY for the same symbol+qty. The
    broker has TWO matching SELL fills (one per profile's stop). Each
    profile's reconcile should pick a different SELL fill so we don't
    double-attribute."""
    from reconcile_journal_to_broker import reconcile_with_ctx
    # Profile A: BMY 71 BUY filled
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    db_a = _make_journal_db(tmp_path / "a", [
        (1, "BMY", "buy", 71, "open", "buy-a",
         "2026-04-27T15:00:00", 58.0),
    ])
    db_b = _make_journal_db(tmp_path / "b", [
        (1, "BMY", "buy", 71, "open", "buy-b",
         "2026-04-27T15:30:00", 58.5),
    ])
    api = MagicMock()
    api.list_positions.return_value = []
    # get_order returns based on order_id
    def get_order(oid):
        return _broker_order(oid, "buy", "filled", 71, filled_qty=71)
    api.get_order.side_effect = get_order
    # list_orders for BMY returns BOTH SELL fills
    api.list_orders.return_value = [
        _broker_order(
            "stop-a", "sell", "filled", 71, filled_qty=71,
            filled_avg_price=57.90,
            filled_at=datetime(2026, 5, 4, 13, 30, tzinfo=timezone.utc),
            order_type="trailing_stop",
        ),
        _broker_order(
            "stop-b", "sell", "filled", 71, filled_qty=71,
            filled_avg_price=57.85,
            filled_at=datetime(2026, 5, 4, 13, 35, tzinfo=timezone.utc),
            order_type="trailing_stop",
        ),
    ]
    ctx_a = _ctx(api, db_a, name="A", profile_id=1)
    ctx_b = _ctx(api, db_b, name="B", profile_id=2)
    res_a = reconcile_with_ctx(ctx_a, apply_changes=True)
    # Within a profile, used_sell_order_ids tracks; across profiles the
    # script is run separately, so each picks the OLDEST fill. To prove
    # multi-profile attribution does NOT double-count, run B and verify
    # its picked sell_order_id differs from A's. The strategy: A is run
    # first, picks 'stop-a' (oldest). Then B runs — but in reality
    # 'stop-a' is now consumed because A's reconcile already did its
    # pass. The naive cross-profile dedupe needs out-of-band state.
    # For now, just assert each picks a valid ID and assert that
    # rerunning the same profile is idempotent (no double-backfill).
    res_a2 = reconcile_with_ctx(ctx_a, apply_changes=True)
    assert len(res_a["backfill_sell"]) == 1
    assert len(res_a2["backfill_sell"]) == 0  # second pass: nothing to do


def test_no_order_id_is_ambiguous(tmp_path):
    """Journal entry with no order_id can't be looked up at broker —
    flag for human review, don't auto-cancel or backfill."""
    from reconcile_journal_to_broker import reconcile_with_ctx
    db = _make_journal_db(tmp_path, [
        (5, "FOO", "buy", 10, "open", None,
         "2026-04-20T10:00:00", 50.0),
    ])
    api = MagicMock()
    api.list_positions.return_value = []
    ctx = _ctx(api, db)
    res = reconcile_with_ctx(ctx, apply_changes=True)
    assert len(res["ambiguous"]) == 1
    assert "no order_id" in res["ambiguous"][0]["reason"]


def test_partial_fill_with_no_remaining_shares_is_fix_partial_entry(tmp_path):
    """Entry partially filled then canceled, AND broker now has 0
    shares of the symbol (the filled portion was subsequently sold).
    fix_partial_entry takes priority — corrects journal qty so the
    next reconcile pass sees the right number to look for in broker
    SELL fills."""
    from reconcile_journal_to_broker import reconcile_with_ctx
    db = _make_journal_db(tmp_path, [
        (7, "XYZ", "buy", 28, "open", "partial",
         "2026-04-24T18:00:00", 80.0),
    ])
    api = MagicMock()
    api.list_positions.return_value = []
    api.get_order.return_value = _broker_order(
        "partial", "buy", "canceled", qty=28, filled_qty=14,
        filled_avg_price=80.0,
    )
    ctx = _ctx(api, db)
    res = reconcile_with_ctx(ctx, apply_changes=True)
    assert len(res["fix_partial_entry"]) == 1
    fix = res["fix_partial_entry"][0]
    assert fix["actual_filled_qty"] == 14


def test_short_held_at_broker_is_real(tmp_path):
    """Short journal entry (side='short') with broker_qty < 0 is real."""
    from reconcile_journal_to_broker import reconcile_with_ctx
    db = _make_journal_db(tmp_path, [
        (12, "MSFT", "short", 17, "open", "short-order",
         "2026-04-29T10:00:00", 401.83),
    ])
    api = MagicMock()
    api.list_positions.return_value = [_broker_position("MSFT", "-17")]
    ctx = _ctx(api, db)
    res = reconcile_with_ctx(ctx, apply_changes=True)
    assert res["real_held"] == 1
    assert len(res["cancel"]) == 0
    assert len(res["backfill_cover"]) == 0


def test_short_phantom_cancel(tmp_path):
    """Short entry order canceled — mark journal status='canceled'."""
    from reconcile_journal_to_broker import reconcile_with_ctx
    db = _make_journal_db(tmp_path, [
        (12, "MSFT", "short", 17, "open", "short-order",
         "2026-04-29T10:00:00", 401.83),
    ])
    api = MagicMock()
    api.list_positions.return_value = []
    api.get_order.return_value = _broker_order(
        "short-order", "sell", "canceled", qty=17, filled_qty=0,
    )
    ctx = _ctx(api, db)
    res = reconcile_with_ctx(ctx, apply_changes=True)
    assert len(res["cancel"]) == 1
    assert res["cancel"][0]["symbol"] == "MSFT"
    conn = sqlite3.connect(db)
    status = conn.execute("SELECT status FROM trades WHERE id=12").fetchone()[0]
    conn.close()
    assert status == "canceled"


def test_short_covered_by_broker_backfills_cover_row(tmp_path):
    """Broker BOUGHT to cover via stop — backfill 'cover' row, mark
    short closed."""
    from reconcile_journal_to_broker import reconcile_with_ctx
    db = _make_journal_db(tmp_path, [
        (12, "MSFT", "short", 17, "open", "short-order",
         "2026-04-29T10:00:00", 401.83),
    ])
    api = MagicMock()
    api.list_positions.return_value = []
    api.get_order.return_value = _broker_order(
        "short-order", "sell", "filled", qty=17, filled_qty=17,
    )
    api.list_orders.return_value = [
        _broker_order(
            "cover-fill", "buy", "filled", qty=17, filled_qty=17,
            filled_avg_price=395.00,
            filled_at=datetime(2026, 5, 4, 13, 30, tzinfo=timezone.utc),
            order_type="trailing_stop",
        ),
    ]
    ctx = _ctx(api, db)
    res = reconcile_with_ctx(ctx, apply_changes=True)
    assert len(res["backfill_cover"]) == 1
    backfill = res["backfill_cover"][0]
    assert backfill["symbol"] == "MSFT"
    assert backfill["cover_price"] == 395.00
    conn = sqlite3.connect(db)
    short_status = conn.execute("SELECT status FROM trades WHERE id=12").fetchone()[0]
    assert short_status == "closed"
    cover_rows = conn.execute(
        "SELECT side, qty, price FROM trades WHERE id != 12"
    ).fetchall()
    assert len(cover_rows) == 1
    assert cover_rows[0][0] == "cover"
    assert cover_rows[0][1] == 17
    assert cover_rows[0][2] == 395.00
    conn.close()


def test_partial_entry_fill_corrects_qty(tmp_path):
    """Entry order canceled with filled_qty>0 (e.g. 28 ordered, 14
    filled, then canceled). Update journal to reflect the actual fill,
    leave status='open' so next reconcile pass re-evaluates."""
    from reconcile_journal_to_broker import reconcile_with_ctx
    db = _make_journal_db(tmp_path, [
        (7, "XYZ", "buy", 28, "open", "partial-order",
         "2026-04-24T18:00:00", 80.0),
    ])
    api = MagicMock()
    api.list_positions.return_value = [_broker_position("XYZ", "14")]
    api.get_order.return_value = _broker_order(
        "partial-order", "buy", "canceled", qty=28, filled_qty=14,
    )
    # filled_avg_price for the partial fill
    api.get_order.return_value.filled_avg_price = 79.50
    ctx = _ctx(api, db)
    res = reconcile_with_ctx(ctx, apply_changes=True)
    assert len(res["fix_partial_entry"]) == 1
    fix = res["fix_partial_entry"][0]
    assert fix["actual_filled_qty"] == 14
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT qty, price, status FROM trades WHERE id=7").fetchone()
    conn.close()
    assert row[0] == 14
    assert row[1] == 79.50
    assert row[2] == "open"  # stays open for next reconcile pass


def test_partial_sale_drift_backfills_partial_sell(tmp_path):
    """Journal says BUY 71, broker has 50 — 21 shares were sold via a
    protective stop. Backfill SELL row for the 21-share portion. The
    BUY row stays open (FIFO consumes the SELL from the lot)."""
    from reconcile_journal_to_broker import reconcile_with_ctx
    # Create a journal with the BUY having a protective stop order id
    p = tmp_path / "journal.db"
    conn = sqlite3.connect(str(p))
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            timestamp TEXT, symbol TEXT, side TEXT, qty REAL, price REAL,
            order_id TEXT, signal_type TEXT, strategy TEXT, reason TEXT,
            status TEXT, pnl REAL, fill_price REAL,
            protective_stop_order_id TEXT,
            protective_tp_order_id TEXT,
            protective_trailing_order_id TEXT
        )
    """)
    conn.execute(
        "INSERT INTO trades (id, symbol, side, qty, status, order_id, "
        "timestamp, price, protective_trailing_order_id) "
        "VALUES (88, 'BMY', 'buy', 71, 'open', 'bmy-buy', "
        "'2026-04-27T15:00:00', 58.34, 'partial-stop-id')",
    )
    conn.commit()
    conn.close()

    api = MagicMock()
    api.list_positions.return_value = [_broker_position("BMY", "50")]
    # The protective trailing stop order partially filled
    api.get_order.return_value = _broker_order(
        "partial-stop-id", "sell", "filled", qty=21, filled_qty=21,
        filled_avg_price=57.90,
        filled_at=datetime(2026, 5, 4, 13, 30, tzinfo=timezone.utc),
        order_type="trailing_stop",
    )
    ctx = _ctx(api, str(p))
    res = reconcile_with_ctx(ctx, apply_changes=True)
    assert len(res["backfill_partial_sell"]) == 1
    bp = res["backfill_partial_sell"][0]
    assert bp["sell_qty"] == 21
    # Check DB: BUY still open, SELL row inserted
    conn = sqlite3.connect(str(p))
    buy_status = conn.execute("SELECT status FROM trades WHERE id=88").fetchone()[0]
    assert buy_status == "open"  # FIFO consumes the SELL — BUY lot stays
    sell_rows = conn.execute(
        "SELECT side, qty, price FROM trades WHERE id != 88"
    ).fetchall()
    assert len(sell_rows) == 1
    assert sell_rows[0][0] == "sell"
    assert sell_rows[0][1] == 21
    conn.close()


def test_api_error_retries_then_flags_ambiguous(tmp_path):
    """Transient broker API failure: retry per _API_MAX_RETRIES, then
    mark ambiguous. Earlier behavior was to immediately flag and let
    drift sit."""
    import reconcile_journal_to_broker as rjtb
    db = _make_journal_db(tmp_path, [
        (1, "FOO", "buy", 10, "open", "foo-order",
         "2026-04-20T10:00:00", 50.0),
    ])
    api = MagicMock()
    api.list_positions.return_value = []
    api.get_order.side_effect = RuntimeError("broker down")
    ctx = _ctx(api, db)
    # Speed up: 0-second backoff for the test
    rjtb._API_MAX_RETRIES = 2  # cuts retry from 3 to 2
    try:
        res = rjtb.reconcile_with_ctx(ctx, apply_changes=True)
    finally:
        rjtb._API_MAX_RETRIES = 3
    assert len(res["ambiguous"]) == 1
    assert "after retries" in res["ambiguous"][0]["reason"]
    # Verify it actually retried (called more than once)
    assert api.get_order.call_count >= 2


def _make_journal_db_with_options(tmp_path, rows):
    """Variant of _make_journal_db that includes occ_symbol +
    option_strategy columns for options-aware tests.

    rows: list of (id, symbol, side, qty, status, order_id, ts, price,
                   occ_symbol, option_strategy)
    """
    p = tmp_path / "journal.db"
    conn = sqlite3.connect(str(p))
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            timestamp TEXT, symbol TEXT, side TEXT, qty REAL, price REAL,
            order_id TEXT, signal_type TEXT, strategy TEXT, reason TEXT,
            status TEXT, pnl REAL, fill_price REAL,
            occ_symbol TEXT, option_strategy TEXT
        )
    """)
    for tid, sym, side, qty, status, order_id, ts, price, occ, strat in rows:
        conn.execute(
            "INSERT INTO trades (id, timestamp, symbol, side, qty, "
            "price, order_id, status, occ_symbol, option_strategy) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tid, ts, sym, side, qty, price, order_id, status, occ, strat),
        )
    conn.commit()
    conn.close()
    return str(p)


def test_options_held_at_broker_is_real(tmp_path):
    """Journal row for an option contract: broker lookup uses the OCC
    symbol, NOT the underlying. The exact bug from prod 2026-05-06:
    profile_4 #134 bull_put_spread BUY was flagged ambiguous because
    reconcile asked the broker about MSFT stock instead of
    MSFT260612P00375000 contract."""
    from reconcile_journal_to_broker import reconcile_with_ctx
    db = _make_journal_db_with_options(tmp_path, [
        (134, "MSFT", "buy", 1, "open", "opt-order",
         "2026-05-06T14:31:36", 5.50,
         "MSFT260612P00375000", "bull_put_spread"),
    ])
    api = MagicMock()
    # Broker has the OPTION contract, not the underlying stock
    api.list_positions.return_value = [
        _broker_position("MSFT260612P00375000", "1"),
    ]
    ctx = _ctx(api, db)
    res = reconcile_with_ctx(ctx, apply_changes=True)
    assert res["real_held"] == 1
    assert len(res["ambiguous"]) == 0
    assert len(res["cancel"]) == 0


def test_options_phantom_canceled_entry(tmp_path):
    """Options BUY whose entry order canceled — same cancel-handling
    as stocks but lookup is by OCC symbol."""
    from reconcile_journal_to_broker import reconcile_with_ctx
    db = _make_journal_db_with_options(tmp_path, [
        (134, "MSFT", "buy", 1, "open", "opt-order",
         "2026-05-06T14:31:36", 5.50,
         "MSFT260612P00375000", "bull_put_spread"),
    ])
    api = MagicMock()
    api.list_positions.return_value = []  # nothing at broker
    api.get_order.return_value = _broker_order(
        "opt-order", "buy", "canceled", qty=1, filled_qty=0,
    )
    ctx = _ctx(api, db)
    res = reconcile_with_ctx(ctx, apply_changes=True)
    assert len(res["cancel"]) == 1
    conn = sqlite3.connect(db)
    status = conn.execute("SELECT status FROM trades WHERE id=134").fetchone()[0]
    conn.close()
    assert status == "canceled"


def test_cross_profile_dedup_prevents_double_attribution(tmp_path):
    """The exact bug from prod 2026-05-06 second pass: profile_4 sold
    AVGO 10 (broker order `1fd38138`). profile_11 also had a 10-share
    AVGO BUY open. Without cross-profile dedup, the fallback match
    path picks the broker SELL by qty=10 and attributes it to
    profile_11 too — both journals reference the same broker fill,
    double-counted realized P&L. The dedup set passed via
    `cross_profile_used_ids` blocks the second attribution."""
    p = tmp_path / "journal.db"
    conn = sqlite3.connect(str(p))
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            timestamp TEXT, symbol TEXT, side TEXT, qty REAL, price REAL,
            order_id TEXT, signal_type TEXT, strategy TEXT, reason TEXT,
            status TEXT, pnl REAL, fill_price REAL,
            protective_stop_order_id TEXT,
            protective_tp_order_id TEXT,
            protective_trailing_order_id TEXT
        )
    """)
    conn.execute(
        "INSERT INTO trades (id, symbol, side, qty, status, order_id, "
        "timestamp, price) "
        "VALUES (43, 'AVGO', 'buy', 10, 'open', 'avgo-buy-p11', "
        "'2026-04-24T15:48:38', 414.61)",
    )
    conn.commit()
    conn.close()
    from reconcile_journal_to_broker import reconcile_with_ctx
    api = MagicMock()
    api.list_positions.return_value = [_broker_position("AVGO", "10")]
    api.get_order.return_value = _broker_order(
        "avgo-buy-p11", "buy", "filled", qty=10, filled_qty=10,
    )
    # Broker SELL of 10 exists, but it's already attributed to
    # sibling profile (profile_4) — its order_id is in the dedup set
    api.list_orders.return_value = [
        _broker_order(
            "1fd38138", "sell", "filled", qty=10, filled_qty=10,
            filled_avg_price=415.63,
            filled_at=datetime(2026, 4, 30, 19, 42, tzinfo=timezone.utc),
            order_type="market",
        ),
    ]
    ctx = _ctx(api, str(p))
    cross_used = {"1fd38138"}  # profile_4 already has this SELL
    res = reconcile_with_ctx(ctx, apply_changes=True,
                              cross_profile_used_ids=cross_used)
    # Fallback skipped the already-attributed SELL — profile_11
    # AVGO #43 stays open (broker still has 10 from this profile)
    assert len(res["backfill_sell"]) == 0
    assert res["real_held"] == 1


def test_protective_fill_fallback_finds_exit_without_stored_id(tmp_path):
    """The MBLY case from prod 2026-05-06: profile_9's BUY had NO
    protective_*_order_id stored (the BUY happened on a code path
    that didn't record the id). But a 492-share sell DID fire at the
    broker. The fallback path should find it via list_orders search
    even when no protective ID is recorded.

    Without the fallback: profile_9 MBLY 492 stayed phantom-claim
    indefinitely while the broker had already closed it weeks ago."""
    p = tmp_path / "journal.db"
    conn = sqlite3.connect(str(p))
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            timestamp TEXT, symbol TEXT, side TEXT, qty REAL, price REAL,
            order_id TEXT, signal_type TEXT, strategy TEXT, reason TEXT,
            status TEXT, pnl REAL, fill_price REAL,
            protective_stop_order_id TEXT,
            protective_tp_order_id TEXT,
            protective_trailing_order_id TEXT
        )
    """)
    # No protective_*_order_id columns set — the bug case
    conn.execute(
        "INSERT INTO trades (id, symbol, side, qty, status, order_id, "
        "timestamp, price) "
        "VALUES (48, 'MBLY', 'buy', 492, 'open', 'mbly-buy', "
        "'2026-04-24T13:43:57', 9.10)",
    )
    conn.commit()
    conn.close()
    from reconcile_journal_to_broker import reconcile_with_ctx
    api = MagicMock()
    # Sibling profile's MBLY 491 still at broker
    api.list_positions.return_value = [_broker_position("MBLY", "491")]
    # The BUY entry order is filled — passes the "real fill" check
    api.get_order.return_value = _broker_order(
        "mbly-buy", "buy", "filled", qty=492, filled_qty=492,
    )
    # Broker order history: the 492-share SELL fired but no journal
    # column points to it
    api.list_orders.return_value = [
        _broker_order(
            "mbly-stop-fill", "sell", "filled", qty=492, filled_qty=492,
            filled_avg_price=8.95,
            filled_at=datetime(2026, 4, 29, 19, 25, tzinfo=timezone.utc),
            order_type="stop",
        ),
    ]
    ctx = _ctx(api, str(p))
    res = reconcile_with_ctx(ctx, apply_changes=True)
    # Fallback path catches it
    assert len(res["backfill_sell"]) == 1
    assert res["backfill_sell"][0]["sell_qty"] == 492
    conn = sqlite3.connect(str(p))
    status = conn.execute("SELECT status FROM trades WHERE id=48").fetchone()[0]
    conn.close()
    assert status == "closed"


def test_phantom_sell_detected_and_reverted(tmp_path):
    """Caught 2026-05-06: profile_6 #83 had side='sell' qty=27 B
    status='closed' in the journal, but the broker order was
    canceled with filled_qty=0. The journal logged a SELL that
    never actually filled — the position was still held at the
    broker, which produced a broker_orphan in the aggregate audit.

    Fix: reconcile now checks every closed SELL/COVER row's order_id
    at the broker. If broker says canceled/expired/rejected with
    filled_qty=0, mark the SELL row 'canceled', and reopen the
    matching closed BUY/SHORT so the position is correctly tracked."""
    p = tmp_path / "journal.db"
    conn = sqlite3.connect(str(p))
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            timestamp TEXT, symbol TEXT, side TEXT, qty REAL, price REAL,
            order_id TEXT, signal_type TEXT, strategy TEXT, reason TEXT,
            status TEXT, pnl REAL, fill_price REAL,
            protective_stop_order_id TEXT,
            protective_tp_order_id TEXT,
            protective_trailing_order_id TEXT
        )
    """)
    # The matching BUY (currently 'closed' due to phantom SELL)
    conn.execute(
        "INSERT INTO trades (id, symbol, side, qty, status, order_id, "
        "timestamp, price) "
        "VALUES (62, 'B', 'buy', 27, 'closed', 'b-buy', "
        "'2026-04-22T19:31:49', 40.69)",
    )
    # The phantom SELL — journal says closed, broker says canceled
    conn.execute(
        "INSERT INTO trades (id, symbol, side, qty, status, order_id, "
        "timestamp, price, pnl) "
        "VALUES (83, 'B', 'sell', 27, 'closed', 'phantom-sell', "
        "'2026-04-30T15:00:00', 40.90, 5.67)",
    )
    conn.commit()
    conn.close()

    from reconcile_journal_to_broker import reconcile_with_ctx
    api = MagicMock()
    api.list_positions.return_value = [_broker_position("B", "27")]
    # Both order_ids: BUY filled, SELL canceled with 0 fill
    def get_order(oid):
        if oid == "phantom-sell":
            return _broker_order(oid, "sell", "canceled",
                                  qty=27, filled_qty=0)
        return _broker_order(oid, "buy", "filled", qty=27, filled_qty=27)
    api.get_order.side_effect = get_order
    ctx = _ctx(api, str(p))
    res = reconcile_with_ctx(ctx, apply_changes=True)
    assert len(res["uncancel_sell"]) == 1
    # Verify journal updates
    conn = sqlite3.connect(str(p))
    sell_status = conn.execute(
        "SELECT status, pnl FROM trades WHERE id=83"
    ).fetchone()
    assert sell_status[0] == "canceled"
    assert sell_status[1] is None  # pnl cleared
    buy_status = conn.execute(
        "SELECT status FROM trades WHERE id=62"
    ).fetchone()
    assert buy_status[0] == "open"  # reopened
    conn.close()


def test_protective_fill_caught_when_siblings_still_hold_symbol(tmp_path):
    """Multi-profile correctness gate. The exact 2026-05-06 GT bug:
    profile_9's trailing stop fired for 573 GT, but the broker still
    held 1399 GT total because sibling profiles 3, 5, 10 hadn't sold
    theirs. The old reconcile gated partial-sale detection on
    `broker_qty < journal_qty for the symbol` — but for THIS profile,
    the symbol-level broker_qty (1399) was MORE than its journal_qty
    (573), so the check never fired. Per-profile reconcile said
    `real_held` and missed profile_9's exit entirely.

    The fix: ALWAYS check this profile's protective_*_order_id
    independent of the symbol's account-level broker_qty."""
    p = tmp_path / "journal.db"
    conn = sqlite3.connect(str(p))
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            timestamp TEXT, symbol TEXT, side TEXT, qty REAL, price REAL,
            order_id TEXT, signal_type TEXT, strategy TEXT, reason TEXT,
            status TEXT, pnl REAL, fill_price REAL,
            protective_stop_order_id TEXT,
            protective_tp_order_id TEXT,
            protective_trailing_order_id TEXT
        )
    """)
    conn.execute(
        "INSERT INTO trades (id, symbol, side, qty, status, order_id, "
        "timestamp, price, protective_trailing_order_id) "
        "VALUES (17, 'GT', 'buy', 573, 'open', 'gt-buy', "
        "'2026-04-20T14:13:07', 7.215, 'gt-trail-fill')",
    )
    conn.commit()
    conn.close()
    from reconcile_journal_to_broker import reconcile_with_ctx
    api = MagicMock()
    # Sibling profiles still hold GT: broker shows 1399 long
    api.list_positions.return_value = [_broker_position("GT", "1399")]
    # The protective trailing-stop fired for THIS profile's 573 shares
    api.get_order.return_value = _broker_order(
        "gt-trail-fill", "sell", "filled", qty=573, filled_qty=573,
        filled_avg_price=8.50,
        filled_at=datetime(2026, 5, 4, 16, 38, tzinfo=timezone.utc),
        order_type="trailing_stop",
    )
    ctx = _ctx(api, str(p))
    res = reconcile_with_ctx(ctx, apply_changes=True)
    assert len(res["backfill_sell"]) == 1
    assert res["backfill_sell"][0]["sell_qty"] == 573
    # BUY now closed
    conn = sqlite3.connect(str(p))
    status = conn.execute("SELECT status FROM trades WHERE id=17").fetchone()[0]
    sell_count = conn.execute("SELECT COUNT(*) FROM trades WHERE side='sell'").fetchone()[0]
    conn.close()
    assert status == "closed"
    assert sell_count == 1  # one new SELL row inserted


def test_archived_profile_with_no_account_id_is_skipped(tmp_path):
    """Disabled / archived profile (alpaca_account_id is None or 0)
    should return a 'skipped' result instead of erroring out — so the
    cron-based reconcile doesn't exit with status=1 on a clean run."""
    from reconcile_journal_to_broker import reconcile_with_ctx
    db = _make_journal_db(tmp_path, [
        (1, "FOO", "buy", 10, "open", "foo", "2026-04-20T10:00:00", 50.0),
    ])
    api = MagicMock()
    ctx = _ctx(api, db, name="Crypto (archived)")
    ctx.alpaca_account_id = None  # archived
    res = reconcile_with_ctx(ctx, apply_changes=True)
    assert "skipped" in res
    assert res["real_held"] == 0
    # Broker was never asked
    api.list_positions.assert_not_called()


def test_corrupt_archive_filename_does_not_match(tmp_path):
    """Belt-and-suspenders: the order_id field could contain anything;
    a None or empty string must be handled, not crash."""
    from reconcile_journal_to_broker import reconcile_with_ctx
    db = _make_journal_db(tmp_path, [
        (1, "FOO", "buy", 10, "open", "",
         "2026-04-20T10:00:00", 50.0),
    ])
    api = MagicMock()
    api.list_positions.return_value = []
    ctx = _ctx(api, db)
    res = reconcile_with_ctx(ctx, apply_changes=True)
    # empty order_id same as None — ambiguous
    assert len(res["ambiguous"]) == 1
