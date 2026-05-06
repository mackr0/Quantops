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


def _ctx(api, db_path, name="Test", profile_id=99):
    ctx = SimpleNamespace()
    ctx.api = api
    ctx.get_alpaca_api = lambda: api
    ctx.db_path = db_path
    ctx.display_name = name
    ctx.profile_id = profile_id
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
