"""Protective orders must journal at PLACEMENT time (2026-05-21).

Background
----------
Pre-fix, `bracket_orders.submit_protective_*` called
`api.submit_order` without writing any journal row — the protective
order_id was stamped onto the entry trade's `protective_*_order_id`
column instead. When the broker autonomously filled the protective
(stop / TP / trailing) order, the reconciler saw a fill with no
matching trade row and tripped the orphan-fill safety net halt.

The fix (per `feedback_no_orphan_broker_fills`): every
`api.submit_order` writes a journal row in the same code path. For
protective orders, that means inserting a `pending_protective`
trades row at placement time. The reconciler then UPDATEs that row
when the broker fills it — no synthesis path, no orphan, no halt.

Tests pin
---------
  1. `submit_protective_stop` writes a `pending_protective` row
     after a successful `api.submit_order(type='stop')`.
  2. `submit_protective_take_profit` does the same for limit orders.
  3. `submit_protective_trailing` does the same, with `price=NULL`
     (trailing stops have no fixed trigger).
  4. The row carries: order_id, symbol, side, qty, signal_type,
     status='pending_protective', and a meaningful `reason`.
  5. If `submit_order` raises, NO journal row is written (we
     can't journal an order that didn't get placed).
  6. If `db_path` is None, the order IS still placed but a WARNING
     is logged about the missing journal write (back-compat path).
  7. The journal write failure (DB error) is non-fatal — the order
     remains placed at the broker.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import sys
from contextlib import closing
from unittest.mock import MagicMock

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def journal_db(tmp_path):
    """Minimal trades-table DB with the columns the placement journaler
    writes to. Mirrors what `journal.init_db` would produce minus
    the columns we don't touch in placement."""
    db = str(tmp_path / "test_protective.db")
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            qty REAL NOT NULL,
            price REAL,
            order_id TEXT,
            signal_type TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            reason TEXT
        );
    """)
    conn.commit()
    conn.close()
    return db


def _fake_api_with_order_id(order_id: str = "test-order-123"):
    """Build a MagicMock Alpaca API whose submit_order returns an
    object with .id == order_id."""
    api = MagicMock()
    order = MagicMock()
    order.id = order_id
    api.submit_order.return_value = order
    return api


def _fetch_journal_row(db_path: str, order_id: str):
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM trades WHERE order_id = ?",
            (order_id,),
        ).fetchone()


# ---------------------------------------------------------------------------
# 1-3. Each placement helper writes a pending_protective row
# ---------------------------------------------------------------------------

class TestPlacementJournals:
    def test_protective_stop_writes_pending_row(self, journal_db):
        from bracket_orders import submit_protective_stop
        api = _fake_api_with_order_id("stop-oid-1")
        out = submit_protective_stop(
            api, "AAPL", qty=100, side="sell",
            stop_price=180.0, db_path=journal_db,
        )
        assert out == "stop-oid-1"
        row = _fetch_journal_row(journal_db, "stop-oid-1")
        assert row is not None, (
            "submit_protective_stop must INSERT a journal row at "
            "placement time. The reconciler relies on this row to "
            "UPDATE on fill — without it, broker stop fills become "
            "orphans and trip the safety-net halt."
        )
        assert row["symbol"] == "AAPL"
        assert row["side"] == "sell"
        assert row["qty"] == 100
        assert row["price"] == 180.0
        assert row["status"] == "pending_protective"
        assert row["signal_type"] == "PROTECTIVE_STOP"
        assert "broker stop" in (row["reason"] or "").lower()

    def test_take_profit_writes_pending_row(self, journal_db):
        from bracket_orders import submit_protective_take_profit
        api = _fake_api_with_order_id("tp-oid-1")
        out = submit_protective_take_profit(
            api, "MSFT", qty=50, side="sell",
            limit_price=420.0, db_path=journal_db,
        )
        assert out == "tp-oid-1"
        row = _fetch_journal_row(journal_db, "tp-oid-1")
        assert row is not None
        assert row["signal_type"] == "PROTECTIVE_TAKE_PROFIT"
        assert row["status"] == "pending_protective"
        assert row["price"] == 420.0

    def test_trailing_stop_writes_pending_row_with_null_price(self, journal_db):
        from bracket_orders import submit_protective_trailing
        api = _fake_api_with_order_id("trail-oid-1")
        out = submit_protective_trailing(
            api, "NVDA", qty=25, side="sell",
            trail_percent=5.0, db_path=journal_db,
        )
        assert out == "trail-oid-1"
        row = _fetch_journal_row(journal_db, "trail-oid-1")
        assert row is not None
        assert row["signal_type"] == "PROTECTIVE_TRAILING"
        assert row["status"] == "pending_protective"
        # Trailing stops have no fixed trigger price
        assert row["price"] is None, (
            "Trailing stops should write price=NULL — the broker "
            "tracks the high-water mark internally, there's no "
            "single trigger price at placement."
        )

    def test_short_cover_protective_uses_buy_side(self, journal_db):
        """For a short position, the protective stop is a BUY-to-cover.
        Journal row should reflect side='buy'."""
        from bracket_orders import submit_protective_stop
        api = _fake_api_with_order_id("short-stop-oid")
        submit_protective_stop(
            api, "TSLA", qty=50, side="buy",  # buy-to-cover
            stop_price=320.0, db_path=journal_db,
        )
        row = _fetch_journal_row(journal_db, "short-stop-oid")
        assert row["side"] == "buy"


# ---------------------------------------------------------------------------
# 4. Entry-trade-id linkage
# ---------------------------------------------------------------------------

class TestEntryTradeIdLinkage:
    def test_reason_includes_entry_trade_id_when_provided(self, journal_db):
        from bracket_orders import submit_protective_trailing
        api = _fake_api_with_order_id("trail-with-entry")
        submit_protective_trailing(
            api, "NVDA", qty=25, side="sell",
            trail_percent=5.0, db_path=journal_db,
            entry_trade_id=42,
        )
        row = _fetch_journal_row(journal_db, "trail-with-entry")
        assert "entry_trade=42" in (row["reason"] or "")

    def test_no_entry_trade_id_still_writes_row(self, journal_db):
        """Back-compat: callers that don't pass entry_trade_id still
        get the row, just without the linkage in the reason."""
        from bracket_orders import submit_protective_trailing
        api = _fake_api_with_order_id("trail-no-entry")
        submit_protective_trailing(
            api, "NVDA", qty=25, side="sell",
            trail_percent=5.0, db_path=journal_db,
            # no entry_trade_id
        )
        row = _fetch_journal_row(journal_db, "trail-no-entry")
        assert row is not None


# ---------------------------------------------------------------------------
# 5. If submit_order raises, no journal row is written
# ---------------------------------------------------------------------------

class TestOrderFailureNoJournal:
    def test_submit_failure_writes_nothing(self, journal_db):
        """If api.submit_order raises, we have no order_id and no
        order at the broker — must not write a journal row that
        references a non-existent order."""
        from bracket_orders import submit_protective_stop
        api = MagicMock()
        api.submit_order.side_effect = Exception("broker error")
        out = submit_protective_stop(
            api, "AAPL", qty=100, side="sell",
            stop_price=180.0, db_path=journal_db,
        )
        assert out is None
        # No rows
        with closing(sqlite3.connect(journal_db)) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM trades"
            ).fetchone()[0]
        assert count == 0


# ---------------------------------------------------------------------------
# 6. Back-compat: db_path=None still places the order, just logs warning
# ---------------------------------------------------------------------------

class TestDbPathOptional:
    def test_no_db_path_still_places_order(self, caplog):
        """The order MUST still be placed at the broker when db_path
        isn't provided — we don't want to skip protective orders just
        because the caller didn't wire the db. A WARNING is logged
        so the operator sees the journal gap."""
        from bracket_orders import submit_protective_stop
        api = _fake_api_with_order_id("no-db-oid")
        with caplog.at_level(logging.WARNING):
            out = submit_protective_stop(
                api, "AAPL", qty=100, side="sell",
                stop_price=180.0, db_path=None,
            )
        assert out == "no-db-oid"
        api.submit_order.assert_called_once()
        # The warning should mention the missing journal write
        warning_msgs = [r.message for r in caplog.records
                          if r.levelname == "WARNING"]
        assert any("db_path is None" in m for m in warning_msgs), (
            "When db_path is None the placement helper should log a "
            "WARNING explaining that the journal write was skipped — "
            "otherwise the operator never finds out their protective "
            "orders are unprotected against the orphan-fill bug."
        )


# ---------------------------------------------------------------------------
# 7. Journal write failure is non-fatal
# ---------------------------------------------------------------------------

class TestJournalWriteFailureNonFatal:
    def test_db_locked_does_not_break_placement(self, tmp_path, caplog):
        """If the journal write fails (e.g., DB locked, disk full),
        the helper should still return the order_id — the broker
        order IS placed; failing the placement would be worse than
        failing the journal write. Reconciler safety-net catches
        the orphan if it ever fires."""
        from bracket_orders import submit_protective_stop
        # Point db_path at a path that can't be written
        bad_path = str(tmp_path / "nonexistent_dir" / "missing.db")
        api = _fake_api_with_order_id("badpath-oid")
        with caplog.at_level(logging.WARNING):
            out = submit_protective_stop(
                api, "AAPL", qty=100, side="sell",
                stop_price=180.0, db_path=bad_path,
            )
        assert out == "badpath-oid", (
            "Placement helper must return the order_id even if the "
            "journal write fails — the broker order IS placed and "
            "the caller needs the id to track it."
        )


# ---------------------------------------------------------------------------
# 8. pending_protective rows must NOT affect positions or cash
# ---------------------------------------------------------------------------

class TestPendingProtectiveIsPassive:
    """The whole point of writing a pending_protective row at
    placement is that it's a PLACEHOLDER — the position is still
    open and no cash has moved until the broker fires the order.
    `get_virtual_positions` and `get_virtual_account_info` must
    ignore these rows until the reconciler flips them to 'closed'."""

    @pytest.fixture
    def full_db(self, tmp_path, monkeypatch):
        """A journal DB with the full trades schema (so
        get_virtual_positions / get_virtual_account_info run)."""
        db = str(tmp_path / "passive.db")
        monkeypatch.chdir(tmp_path)
        from journal import init_db
        init_db(db)
        return db

    def _insert(self, db, **cols):
        keys = ", ".join(cols.keys())
        qs = ", ".join("?" * len(cols))
        with closing(sqlite3.connect(db)) as conn:
            conn.execute(
                f"INSERT INTO trades ({keys}) VALUES ({qs})",
                tuple(cols.values()),
            )
            conn.commit()

    def test_pending_protective_does_not_close_position(self, full_db):
        from journal import get_virtual_positions
        # Open BUY 100 AAPL @ $180
        self._insert(full_db, timestamp="2026-05-21T10:00:00",
                     symbol="AAPL", side="buy", qty=100, price=180.0,
                     status="open", signal_type="BUY")
        # Pending protective stop SELL 100 AAPL @ trigger $171
        self._insert(full_db, timestamp="2026-05-21T10:00:01",
                     symbol="AAPL", side="sell", qty=100, price=171.0,
                     order_id="prot-1", status="pending_protective",
                     signal_type="PROTECTIVE_STOP")
        positions = get_virtual_positions(
            db_path=full_db, price_fetcher=lambda s: 185.0)
        aapl = [p for p in positions if p["symbol"] == "AAPL"]
        assert len(aapl) == 1, (
            "The pending protective SELL must NOT FIFO-close the open "
            "BUY — the position is still held until the broker fires "
            "the stop. Got positions: " + str(positions)
        )
        assert aapl[0]["qty"] == 100

    def test_pending_protective_does_not_move_cash(self, full_db):
        from journal import get_virtual_account_info
        # Open BUY 100 AAPL @ $180 → cash out $18,000
        self._insert(full_db, timestamp="2026-05-21T10:00:00",
                     symbol="AAPL", side="buy", qty=100, price=180.0,
                     status="open", signal_type="BUY")
        # Pending protective stop with trigger $171 — must NOT add
        # $17,100 of phantom cash-in.
        self._insert(full_db, timestamp="2026-05-21T10:00:01",
                     symbol="AAPL", side="sell", qty=100, price=171.0,
                     order_id="prot-1", status="pending_protective",
                     signal_type="PROTECTIVE_STOP")
        info = get_virtual_account_info(
            db_path=full_db, initial_capital=100000.0,
            price_fetcher=lambda s: 185.0)
        # cash = 100000 - 18000 (buy) + 0 (pending is ignored) = 82000
        assert abs(info["cash"] - 82000.0) < 0.01, (
            f"Pending protective row inflated cash: got {info['cash']}, "
            "expected 82000. The trigger price must NOT count as a "
            "real cash flow until the broker fires the order."
        )

    def test_closed_protective_DOES_count(self, full_db):
        """Once the reconciler flips the row to 'closed' with the
        actual fill, it participates in cash + position math normally."""
        from journal import get_virtual_account_info, get_virtual_positions
        self._insert(full_db, timestamp="2026-05-21T10:00:00",
                     symbol="AAPL", side="buy", qty=100, price=180.0,
                     status="closed", signal_type="BUY")
        # Reconciler flipped the protective to closed @ actual fill $171
        self._insert(full_db, timestamp="2026-05-21T14:00:00",
                     symbol="AAPL", side="sell", qty=100, price=171.0,
                     order_id="prot-1", status="closed",
                     signal_type="reconcile_backfill")
        info = get_virtual_account_info(
            db_path=full_db, initial_capital=100000.0)
        # cash = 100000 - 18000 + 17100 = 99100
        assert abs(info["cash"] - 99100.0) < 0.01
        # No open positions
        positions = get_virtual_positions(db_path=full_db)
        assert [p for p in positions if p["symbol"] == "AAPL"] == []
