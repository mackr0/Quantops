"""Order_id-keyed protective-order sync (2026-05-21).

The canonical key for "is this position protected?" is the Alpaca
order_id, not a re-derived symbol+side journal lookup. These tests
pin the three pieces of the real fix:

  1. `active_protective_coverage` reads broker truth — buckets live
     stop/trailing_stop orders by (symbol, side).
  2. `ensure_protective_stops` decides skip-vs-place against that
     broker coverage (NOT a fuzzy journal row), and HEALS the
     journal entry-row pointer to the live order_id. This kills the
     FCX-class bug: a symbol held as BOTH stock and option legs no
     longer causes endless "insufficient qty available" retries on
     an already-protected position.
  3. `verify_protective_order_sync` — the invariant: every protective
     order_id the journal records as active must be live at Alpaca.
     Stale linkage is flagged deterministically.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing
from unittest.mock import MagicMock

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


def _order(symbol, side, qty, otype="trailing_stop", status="new", oid=None):
    o = MagicMock()
    o.symbol = symbol
    o.side = side
    o.qty = qty
    o.order_type = otype
    o.status = status
    o.id = oid or f"{symbol}-{side}-{otype}"
    return o


@pytest.fixture
def full_db(tmp_path):
    """trades table with occ_symbol + protective columns (prod shape)."""
    db = str(tmp_path / "t.db")
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            timestamp TEXT, symbol TEXT, side TEXT, qty REAL, price REAL,
            order_id TEXT, signal_type TEXT, status TEXT, occ_symbol TEXT,
            protective_stop_order_id TEXT,
            protective_tp_order_id TEXT,
            protective_trailing_order_id TEXT
        )
    """)
    conn.commit()
    conn.close()
    return db


def _ctx(use_trailing=True):
    c = MagicMock()
    c.stop_loss_pct = 0.05
    c.short_stop_loss_pct = 0.08
    c.take_profit_pct = None
    c.short_take_profit_pct = None
    c.use_trailing_stops = use_trailing
    return c


# ---------------------------------------------------------------------------
# 1. active_protective_coverage
# ---------------------------------------------------------------------------

class TestActiveProtectiveCoverage:
    def test_buckets_by_symbol_and_side(self):
        from bracket_orders import active_protective_coverage
        api = MagicMock()
        api.list_orders.return_value = [
            _order("FCX", "sell", 418, "trailing_stop", oid="fcx-1"),
            _order("AMZN", "sell", 76, "trailing_stop", oid="amzn-1"),
            _order("AMZN", "sell", 69, "trailing_stop", oid="amzn-2"),
            # A non-protective order is ignored
            _order("AAPL", "buy", 10, "market", oid="aapl-mkt"),
            # An inactive protective order is ignored
            _order("NVDA", "sell", 5, "stop", status="filled", oid="nvda-x"),
        ]
        cov = active_protective_coverage(api)
        assert {c["order_id"] for c in cov[("FCX", "sell")]} == {"fcx-1"}
        assert sum(c["qty"] for c in cov[("AMZN", "sell")]) == 145
        assert ("AAPL", "buy") not in cov  # market order excluded
        assert ("NVDA", "sell") not in cov  # filled excluded

    def test_api_error_returns_empty(self):
        from bracket_orders import active_protective_coverage
        api = MagicMock()
        api.list_orders.side_effect = Exception("boom")
        assert active_protective_coverage(api) == {}


# ---------------------------------------------------------------------------
# 2. ensure_protective_stops skip + heal against broker truth
# ---------------------------------------------------------------------------

class TestBrokerTruthSkipAndHeal:
    def test_skips_when_broker_already_covers(self, full_db):
        """Broker has a live trailing stop covering the full position
        → no new placement, even if the journal pointer is missing."""
        from bracket_orders import ensure_protective_stops
        # Stock entry row with NO protective pointer recorded (stale)
        conn = sqlite3.connect(full_db)
        conn.execute(
            "INSERT INTO trades (id, symbol, side, qty, price, status, "
            "occ_symbol) VALUES (49, 'FCX', 'buy', 418, 60.2, 'open', NULL)")
        conn.commit()
        conn.close()

        api = MagicMock()
        api.list_orders.return_value = [
            _order("FCX", "sell", 418, "trailing_stop", oid="fcx-live"),
        ]
        positions = [{"symbol": "FCX", "qty": 418, "avg_entry_price": 60.2}]
        ensure_protective_stops(api, positions, _ctx(), full_db)

        # No new protective order placed
        api.submit_order.assert_not_called()
        # Journal pointer healed to the live broker order
        conn = sqlite3.connect(full_db)
        healed = conn.execute(
            "SELECT protective_trailing_order_id FROM trades WHERE id=49"
        ).fetchone()[0]
        conn.close()
        assert healed == "fcx-live", (
            "Entry row's protective pointer should be healed to the "
            "live broker order_id so journal == Alpaca."
        )

    def test_fcx_class_stock_plus_option_legs(self, full_db):
        """The exact prod scenario: FCX held as a 418-share stock
        position AND as option legs. Broker has the stock's trailing
        stop. The sweep must skip (not retry placement) — proving the
        decision is broker-truth-keyed, not 'newest journal row'."""
        from bracket_orders import ensure_protective_stops
        conn = sqlite3.connect(full_db)
        # Stock entry (older id) WITH the pointer recorded
        conn.execute(
            "INSERT INTO trades (id, symbol, side, qty, price, status, "
            "occ_symbol, protective_trailing_order_id) "
            "VALUES (49, 'FCX', 'buy', 418, 60.2, 'open', NULL, 'fcx-live')")
        # Newer option leg rows (would win ORDER BY id DESC)
        conn.execute(
            "INSERT INTO trades (id, symbol, side, qty, price, status, "
            "occ_symbol) VALUES (53, 'FCX', 'buy', 2, 2.14, 'open', "
            "'FCX260626C00067000')")
        conn.commit()
        conn.close()

        api = MagicMock()
        api.list_orders.return_value = [
            _order("FCX", "sell", 418, "trailing_stop", oid="fcx-live"),
        ]
        positions = [{"symbol": "FCX", "qty": 418, "avg_entry_price": 60.2}]
        ensure_protective_stops(api, positions, _ctx(), full_db)
        api.submit_order.assert_not_called()

    def test_places_when_broker_has_no_coverage(self, full_db):
        """No broker protective order → place one (normal path)."""
        from bracket_orders import ensure_protective_stops
        conn = sqlite3.connect(full_db)
        conn.execute(
            "INSERT INTO trades (id, symbol, side, qty, price, status, "
            "occ_symbol) VALUES (1, 'AAPL', 'buy', 100, 150.0, 'open', NULL)")
        conn.commit()
        conn.close()

        api = MagicMock()
        api.list_orders.return_value = []  # no coverage
        api.submit_order.return_value = MagicMock(id="new-trail")
        positions = [{"symbol": "AAPL", "qty": 100, "avg_entry_price": 150.0}]
        ensure_protective_stops(api, positions, _ctx(), full_db)
        assert api.submit_order.call_count == 1
        assert api.submit_order.call_args.kwargs["type"] == "trailing_stop"


# ---------------------------------------------------------------------------
# 3. verify_protective_order_sync invariant
# ---------------------------------------------------------------------------

class TestVerifyProtectiveOrderSync:
    def test_stale_pointer_flagged(self, full_db):
        """Journal records a protective order_id that's NOT live at
        Alpaca → flagged as stale."""
        from bracket_orders import verify_protective_order_sync
        conn = sqlite3.connect(full_db)
        conn.execute(
            "INSERT INTO trades (id, symbol, side, qty, price, status, "
            "protective_trailing_order_id) "
            "VALUES (1, 'FCX', 'buy', 418, 60.2, 'open', 'dead-order')")
        conn.commit()
        conn.close()

        api = MagicMock()
        api.get_order.return_value = MagicMock(status="canceled")
        out = verify_protective_order_sync(api, full_db)
        assert len(out["stale"]) == 1
        assert out["stale"][0]["order_id"] == "dead-order"
        assert out["stale"][0]["symbol"] == "FCX"

    def test_live_pointer_verified(self, full_db):
        from bracket_orders import verify_protective_order_sync
        conn = sqlite3.connect(full_db)
        conn.execute(
            "INSERT INTO trades (id, symbol, side, qty, price, status, "
            "protective_trailing_order_id) "
            "VALUES (1, 'FCX', 'buy', 418, 60.2, 'open', 'live-order')")
        conn.commit()
        conn.close()
        api = MagicMock()
        api.get_order.return_value = MagicMock(status="new")
        out = verify_protective_order_sync(api, full_db)
        assert out["stale"] == []
        assert out["verified"] == 1

    def test_pending_protective_row_checked(self, full_db):
        """A pending_protective row whose order isn't live → stale."""
        from bracket_orders import verify_protective_order_sync
        conn = sqlite3.connect(full_db)
        conn.execute(
            "INSERT INTO trades (id, symbol, side, qty, price, status, "
            "order_id, signal_type) "
            "VALUES (1, 'FCX', 'sell', 418, NULL, 'pending_protective', "
            "'pp-dead', 'PROTECTIVE_TRAILING')")
        conn.commit()
        conn.close()
        api = MagicMock()
        api.get_order.return_value = MagicMock(status="filled")
        out = verify_protective_order_sync(api, full_db)
        assert len(out["stale"]) == 1
        assert out["stale"][0]["order_id"] == "pp-dead"

    def test_no_db_path_returns_empty(self):
        from bracket_orders import verify_protective_order_sync
        out = verify_protective_order_sync(MagicMock(), None)
        assert out == {"stale": [], "verified": 0}
