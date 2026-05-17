"""Perfect-matching invariant: every journal trade row's `order_id`
must equal the trading system's order ID (broker-agnostic).

User mandate after the reconciler caused profile-attribution
heuristics + drift confusion: "if we are going to start over, I
need to know we match order IDs, whatever system we are working
with." The journal's `order_id` column is the single point of
correspondence between virtual profile and the actual order
submitted to the trading system. If it's missing or duplicate or
synthetic, reconciliation breaks.

Three structural tests:

1. **No missing order_id on live rows.** Every row with
   status='open' or 'pending_fill' must have an `order_id` UNLESS
   it's an explicit auto-reconcile sentinel. A live row without
   one is an unattributable position.

2. **No duplicate order_id within a profile.** Two rows in one
   profile's journal can share an order_id only when they're the
   legs of the same multileg combo (in which case occ_symbol
   distinguishes them). Same (order_id, occ_symbol) duplication
   means a double-write bug.

3. **No order_id collision across profiles routing to the same
   broker account.** If two profiles share account 2 and both
   journals reference the same `order_id` for the same leg, one
   of them is wrong.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


_LIVE_STATUSES = ("open", "pending_fill")
_SENTINEL_SIGNAL_TYPES = (
    "AUTO_RECONCILE", "AUTO_RECONCILE_PHANTOM_CLOSE",
)


def _trades_schema():
    return """
    CREATE TABLE trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT DEFAULT (datetime('now')),
        symbol TEXT, side TEXT, qty REAL, price REAL,
        fill_price REAL,
        order_id TEXT,
        signal_type TEXT,
        status TEXT DEFAULT 'open',
        occ_symbol TEXT
    )
    """


@pytest.fixture
def db(tmp_path):
    """Use the production schema (via journal.init_db) so log_trade
    INSERTs match every expected column. The local minimal schema
    is too tight for log_trade's INSERT shape."""
    p = str(tmp_path / "trades.db")
    from journal import init_db
    init_db(p)
    return p


def _check_no_live_row_missing_order_id(db_path):
    """Returns list of (id, side, qty, symbol, signal_type, status)
    for live rows that lack an order_id. Empty list = invariant holds."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # Live row + not a sentinel + no order_id
        sentinel_placeholders = ",".join(["?"] * len(_SENTINEL_SIGNAL_TYPES))
        rows = conn.execute(
            f"SELECT id, side, qty, symbol, signal_type, status "
            f"FROM trades "
            f"WHERE COALESCE(status,'open') IN ('open', 'pending_fill') "
            f"  AND (order_id IS NULL OR order_id = '') "
            f"  AND (signal_type IS NULL "
            f"       OR signal_type NOT IN ({sentinel_placeholders}))",
            _SENTINEL_SIGNAL_TYPES,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _check_no_duplicate_within_profile(db_path):
    """Returns list of (order_id, occ_symbol, count) for tuples
    duplicated within one profile's trades table. (order_id, occ_symbol)
    is the unique-leg identifier — multileg combos legitimately share
    order_id but have distinct occ_symbols."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT order_id, occ_symbol, COUNT(*) AS n "
            "FROM trades "
            "WHERE order_id IS NOT NULL AND order_id != '' "
            "GROUP BY order_id, COALESCE(occ_symbol, '') "
            "HAVING COUNT(*) > 1"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


class TestNoMissingOrderId:

    def test_live_row_without_order_id_is_flagged(self, db):
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO trades (symbol, side, qty, price, status, signal_type) "
            "VALUES ('AAPL', 'buy', 10, 100, 'open', 'BUY')"
        )
        conn.commit()
        conn.close()
        bad = _check_no_live_row_missing_order_id(db)
        assert len(bad) == 1
        assert bad[0]["symbol"] == "AAPL"

    def test_live_row_with_order_id_passes(self, db):
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO trades (symbol, side, qty, price, status, "
            "                     signal_type, order_id) "
            "VALUES ('AAPL', 'buy', 10, 100, 'open', 'BUY', 'real-id-1')"
        )
        conn.commit()
        conn.close()
        assert _check_no_live_row_missing_order_id(db) == []

    def test_auto_reconcile_sentinel_allowed(self, db):
        """Sentinel signal types are explicit exceptions — they
        represent rows backfilled when no real order existed."""
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO trades (symbol, side, qty, price, status, "
            "                     signal_type, order_id) "
            "VALUES ('AAPL', 'buy', 10, 100, 'open', "
            "        'AUTO_RECONCILE', 'auto_reconcile')"
        )
        conn.commit()
        conn.close()
        assert _check_no_live_row_missing_order_id(db) == []

    def test_closed_row_without_order_id_is_NOT_flagged(self, db):
        """Closed rows are historical; the invariant only applies
        to live (open/pending) rows where reconciliation matters."""
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO trades (symbol, side, qty, price, status, "
            "                     signal_type) "
            "VALUES ('AAPL', 'buy', 10, 100, 'closed', 'BUY')"
        )
        conn.commit()
        conn.close()
        assert _check_no_live_row_missing_order_id(db) == []


class TestNoDuplicateOrderId:

    def test_two_rows_same_order_id_same_occ_flagged(self, db):
        """Double-write bug: same combo's same leg written twice."""
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO trades (symbol, side, qty, price, order_id, occ_symbol) "
            "VALUES ('AAPL', 'buy', 1, 1, 'combo-1', 'AAPL260618C00200000')"
        )
        conn.execute(
            "INSERT INTO trades (symbol, side, qty, price, order_id, occ_symbol) "
            "VALUES ('AAPL', 'buy', 1, 1, 'combo-1', 'AAPL260618C00200000')"
        )
        conn.commit()
        conn.close()
        dupes = _check_no_duplicate_within_profile(db)
        assert len(dupes) == 1
        assert dupes[0]["order_id"] == "combo-1"

    def test_multileg_combo_legs_with_different_occ_pass(self, db):
        """Both legs of a bull put spread share order_id but have
        distinct occ_symbols — legitimate, must pass."""
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO trades (symbol, side, qty, price, order_id, occ_symbol) "
            "VALUES ('AAPL', 'sell', 1, 1.5, 'combo-2', 'AAPL260618P00190000')"
        )
        conn.execute(
            "INSERT INTO trades (symbol, side, qty, price, order_id, occ_symbol) "
            "VALUES ('AAPL', 'buy', 1, 1.0, 'combo-2', 'AAPL260618P00185000')"
        )
        conn.commit()
        conn.close()
        assert _check_no_duplicate_within_profile(db) == []


class TestLogTradeWarnsOnMissingOrderId:
    """The runtime guard in log_trade should WARN when called
    without order_id for a non-sentinel row."""

    def test_log_trade_warns_when_order_id_missing(self, db, caplog):
        import logging
        import journal
        # log_trade uses the global DB connection lookup — point it
        # at our fixture by setting the env DB_PATH.
        with caplog.at_level(logging.WARNING):
            journal.log_trade(
                symbol="AAPL", side="buy", qty=10, price=100,
                signal_type="BUY", db_path=db,
            )
        warns = [r for r in caplog.records
                 if "BROKER ORDER ID MISSING" in r.message]
        assert len(warns) == 1

    def test_log_trade_silent_when_order_id_present(self, db, caplog):
        import logging
        import journal
        with caplog.at_level(logging.WARNING):
            journal.log_trade(
                symbol="AAPL", side="buy", qty=10, price=100,
                signal_type="BUY", order_id="real-id-1", db_path=db,
            )
        warns = [r for r in caplog.records
                 if "BROKER ORDER ID MISSING" in r.message]
        assert warns == []

    def test_log_trade_silent_for_auto_reconcile_without_order_id(
        self, db, caplog,
    ):
        import logging
        import journal
        with caplog.at_level(logging.WARNING):
            journal.log_trade(
                symbol="AAPL", side="buy", qty=10, price=100,
                signal_type="AUTO_RECONCILE", db_path=db,
            )
        warns = [r for r in caplog.records
                 if "BROKER ORDER ID MISSING" in r.message]
        assert warns == [], (
            "AUTO_RECONCILE is an explicit sentinel — should NOT warn"
        )
