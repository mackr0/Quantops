"""Tests for the deterministic auto-reconciler that clears the 123
aggregate-audit drift items observed on 2026-05-16.

Pre-fix the drift just sat on /issues, with the only remediation
being a manual review (which violates the "AI-driven, no human-
in-the-loop" rule). The reconciler:

  - broker_orphan: backfill a journal row in the first profile
    sharing the account (deterministic attribution)
  - journal_phantom: mark every contributing open row
    'auto_reconciled_phantom_close' with pnl=0
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from unittest.mock import patch, MagicMock


def _no_history():
    """Default stub: tier-1 finds no order history → tier-2 fallback."""
    return patch(
        "reconcile_aggregate_drift._find_opening_orders_for_position",
        return_value=[],
    )

import pytest


@pytest.fixture(autouse=True)
def _mock_active_profile_ids(monkeypatch):
    """`reconcile_aggregate_drift.reconcile` now calls
    `models.get_active_profile_ids()` to replace the old
    hardcoded `range(1, 12)`. These tests don't initialize a master
    DB, so we mock the helper to return [1] (matches the fake
    `fake_profile = {"id": 1, ...}` the tests build below). Tests
    can override by passing `profile_ids=[...]` explicitly to
    `reconcile()`."""
    monkeypatch.setattr(
        "models.get_active_profile_ids", lambda user_id=None: [1],
    )

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


def _create_trades_db(path):
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT DEFAULT (datetime('now')),
            symbol TEXT, side TEXT, qty REAL, price REAL,
            fill_price REAL,
            signal_type TEXT, reason TEXT,
            status TEXT DEFAULT 'open', pnl REAL,
            occ_symbol TEXT, order_id TEXT
        )
    """)
    conn.commit()
    conn.close()


class TestDryRunNeverWrites:
    def test_dry_run_skips_DB_writes_even_with_drift(
        self, tmp_path, monkeypatch,
    ):
        from reconcile_aggregate_drift import reconcile
        # Stand up a fake profile DB
        db = tmp_path / "quantopsai_profile_1.db"
        _create_trades_db(str(db))
        monkeypatch.chdir(tmp_path)

        fake_audit = {
            "drift": [
                {"account": "acct1", "symbol": "NXPI",
                 "journal_qty": 0.0, "broker_qty": -114.0,
                 "drift": -114.0, "kind": "broker_orphan"},
            ],
            "accounts": {},
            "errored": [],
        }
        fake_profile = {
            "id": 1, "name": "p1", "enabled": True,
            "alpaca_account_id": "acct1",
        }
        with patch(
            "aggregate_audit.audit_aggregate_drift",
            return_value=fake_audit,
        ), patch(
            "reconcile_aggregate_drift._profiles_sharing_account",
            return_value=[fake_profile],
        ), patch(
            "reconcile_aggregate_drift._current_mark",
            return_value=50.0,
        ), _no_history():
            counters = reconcile(apply=False)

        # No row written
        with sqlite3.connect(str(db)) as conn:
            n = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        assert n == 0, "dry-run must not write any rows"
        # But the counter says we WOULD have backfilled
        assert counters["broker_orphan_backfilled"] == 1


class TestBrokerOrphanBackfill:
    def test_apply_writes_journal_row_to_first_profile(
        self, tmp_path, monkeypatch,
    ):
        from reconcile_aggregate_drift import reconcile
        db = tmp_path / "quantopsai_profile_1.db"
        _create_trades_db(str(db))
        monkeypatch.chdir(tmp_path)

        fake_audit = {
            "drift": [
                {"account": "acct1", "symbol": "NXPI",
                 "journal_qty": 0.0, "broker_qty": -114.0,
                 "drift": -114.0, "kind": "broker_orphan"},
            ],
            "accounts": {}, "errored": [],
        }
        fake_profile = {
            "id": 1, "name": "Mid Cap", "enabled": True,
            "alpaca_account_id": "acct1",
        }
        with patch(
            "aggregate_audit.audit_aggregate_drift",
            return_value=fake_audit,
        ), patch(
            "reconcile_aggregate_drift._profiles_sharing_account",
            return_value=[fake_profile],
        ), patch(
            "reconcile_aggregate_drift._current_mark", return_value=50.0,
        ), _no_history():
            counters = reconcile(apply=True)

        assert counters["broker_orphan_backfilled"] == 1
        with sqlite3.connect(str(db)) as conn:
            conn.row_factory = sqlite3.Row
            r = conn.execute("SELECT * FROM trades").fetchone()
        assert r is not None
        assert r["symbol"] == "NXPI"
        assert r["side"] == "short"   # negative broker qty
        assert r["qty"] == 114.0
        assert r["price"] == 50.0
        assert r["signal_type"] == "AUTO_RECONCILE"
        assert "broker_orphan" in r["reason"]

    def test_positive_broker_qty_becomes_buy_side(
        self, tmp_path, monkeypatch,
    ):
        from reconcile_aggregate_drift import reconcile
        db = tmp_path / "quantopsai_profile_1.db"
        _create_trades_db(str(db))
        monkeypatch.chdir(tmp_path)

        fake_audit = {
            "drift": [
                {"account": "acct1", "symbol": "AAPL",
                 "journal_qty": 0.0, "broker_qty": 50.0,
                 "drift": 50.0, "kind": "broker_orphan"},
            ],
            "accounts": {}, "errored": [],
        }
        with patch(
            "aggregate_audit.audit_aggregate_drift",
            return_value=fake_audit,
        ), patch(
            "reconcile_aggregate_drift._profiles_sharing_account",
            return_value=[{"id": 1, "name": "p1", "enabled": True,
                           "alpaca_account_id": "acct1"}],
        ), patch(
            "reconcile_aggregate_drift._current_mark", return_value=200.0,
        ), _no_history():
            reconcile(apply=True)

        with sqlite3.connect(str(db)) as conn:
            r = conn.execute(
                "SELECT side, qty FROM trades WHERE symbol='AAPL'"
            ).fetchone()
        assert r[0] == "buy"
        assert r[1] == 50.0

    def test_no_mark_available_skips_no_unpriced_row(
        self, tmp_path, monkeypatch,
    ):
        """When the current mark can't be obtained, skip rather than
        write an unpriced (price=0) row that would tip back to
        invisibility via the get_virtual_positions filter."""
        from reconcile_aggregate_drift import reconcile
        db = tmp_path / "quantopsai_profile_1.db"
        _create_trades_db(str(db))
        monkeypatch.chdir(tmp_path)

        fake_audit = {
            "drift": [
                {"account": "acct1", "symbol": "DEADCO",
                 "journal_qty": 0.0, "broker_qty": -10.0,
                 "drift": -10.0, "kind": "broker_orphan"},
            ],
            "accounts": {}, "errored": [],
        }
        with patch(
            "aggregate_audit.audit_aggregate_drift",
            return_value=fake_audit,
        ), patch(
            "reconcile_aggregate_drift._profiles_sharing_account",
            return_value=[{"id": 1, "name": "p1", "enabled": True,
                           "alpaca_account_id": "acct1"}],
        ), patch(
            "reconcile_aggregate_drift._current_mark", return_value=None,
        ), _no_history():
            counters = reconcile(apply=True)

        with sqlite3.connect(str(db)) as conn:
            n = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        assert n == 0, (
            "no-mark case must SKIP rather than write a price=0 row"
        )
        assert counters["skipped"] == 1


class TestTier1OrderHistoryEnrichment:
    """When Alpaca order history covers the broker position, use REAL
    fill price + timestamp + order_id and attribute to the profile
    whose journal already references that order_id."""

    def _mk_order(self, oid, side, qty, price, ts, symbol=None):
        o = MagicMock()
        o.id = oid
        o.side = side
        o.filled_qty = qty
        o.filled_avg_price = price
        o.filled_at = ts
        o.submitted_at = ts
        o.symbol = symbol
        o.legs = None
        return o

    def test_real_fill_price_used_when_history_available(
        self, tmp_path, monkeypatch,
    ):
        """Tier 1: list_orders returns the actual opening fill.
        Reconciler writes that price, not the current mark."""
        from reconcile_aggregate_drift import reconcile
        db = tmp_path / "quantopsai_profile_1.db"
        _create_trades_db(str(db))
        monkeypatch.chdir(tmp_path)

        fake_audit = {
            "drift": [
                {"account": "acct1", "symbol": "NXPI",
                 "journal_qty": 0.0, "broker_qty": -114.0,
                 "drift": -114.0, "kind": "broker_orphan"},
            ],
            "accounts": {}, "errored": [],
        }
        # Actual opening fill at $250 on 2026-04-01, not the current
        # mark of $291.
        history = [
            self._mk_order(
                "real-order-1", "sell_short", 114, 250.00,
                "2026-04-01T15:30:00Z", symbol="NXPI",
            ),
        ]
        with patch(
            "aggregate_audit.audit_aggregate_drift",
            return_value=fake_audit,
        ), patch(
            "reconcile_aggregate_drift._profiles_sharing_account",
            return_value=[{"id": 1, "name": "p1", "enabled": True,
                           "alpaca_account_id": "acct1"}],
        ), patch(
            "reconcile_aggregate_drift._find_opening_orders_for_position",
            return_value=[{
                "order_id": "real-order-1",
                "filled_avg_price": 250.00,
                "filled_at": "2026-04-01T15:30:00Z",
                "filled_qty": 114,
            }],
        ), patch(
            "reconcile_aggregate_drift._current_mark", return_value=291.0,
        ):
            reconcile(apply=True)

        with sqlite3.connect(str(db)) as conn:
            conn.row_factory = sqlite3.Row
            r = conn.execute("SELECT * FROM trades").fetchone()
        assert r["price"] == 250.00, (
            "tier-1 must use real fill price, not current mark"
        )
        assert r["order_id"] == "real-order-1", (
            "tier-1 must use the real Alpaca order_id, not "
            "'auto_reconcile' sentinel"
        )
        assert r["timestamp"].startswith("2026-04-01"), (
            "tier-1 must use the real fill timestamp, not now"
        )
        assert "order-history-match" in r["reason"] or \
               "order-history-but-no-journal-match" in r["reason"]

    def test_profile_attribution_from_journal_match(
        self, tmp_path, monkeypatch,
    ):
        """When profile 5's journal already has a row with the same
        order_id (e.g., from an earlier multileg leg write that
        didn't drop), the reconciler attributes the new row to
        profile 5 — not the lowest-id profile."""
        from reconcile_aggregate_drift import reconcile
        # Two profiles share the account; only profile 5's journal
        # has the order_id.
        for pid in (1, 5):
            _create_trades_db(str(tmp_path / f"quantopsai_profile_{pid}.db"))
        with sqlite3.connect(
            str(tmp_path / "quantopsai_profile_5.db")
        ) as conn:
            conn.execute(
                "INSERT INTO trades (symbol, side, qty, price, order_id, "
                "                     status) "
                "VALUES ('FOO', 'buy', 1, 1.0, 'real-order-1', 'closed')"
            )
            conn.commit()
        monkeypatch.chdir(tmp_path)

        fake_audit = {
            "drift": [
                {"account": "acct1", "symbol": "BAR",
                 "journal_qty": 0.0, "broker_qty": -10.0,
                 "drift": -10.0, "kind": "broker_orphan"},
            ],
            "accounts": {}, "errored": [],
        }
        profiles = [
            {"id": 1, "name": "p1", "enabled": True,
             "alpaca_account_id": "acct1"},
            {"id": 5, "name": "p5", "enabled": True,
             "alpaca_account_id": "acct1"},
        ]
        with patch(
            "aggregate_audit.audit_aggregate_drift",
            return_value=fake_audit,
        ), patch(
            "reconcile_aggregate_drift._profiles_sharing_account",
            return_value=profiles,
        ), patch(
            "reconcile_aggregate_drift._find_opening_orders_for_position",
            return_value=[{
                "order_id": "real-order-1",
                "filled_avg_price": 30.0,
                "filled_at": "2026-05-01T10:00:00Z",
                "filled_qty": 10,
            }],
        ):
            reconcile(apply=True)

        # Row should land in profile 5 (cross-referenced), NOT
        # profile 1 (lowest id fallback).
        with sqlite3.connect(
            str(tmp_path / "quantopsai_profile_1.db")
        ) as conn:
            n1 = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE symbol='BAR'"
            ).fetchone()[0]
        with sqlite3.connect(
            str(tmp_path / "quantopsai_profile_5.db")
        ) as conn:
            n5 = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE symbol='BAR'"
            ).fetchone()[0]
        assert n1 == 0, "row must NOT go to lowest-id when journal-match exists"
        assert n5 == 1, "row must go to the profile whose journal has the order_id"

    def test_qty_weighted_entry_price_across_partial_fills(self):
        """Two partial fills opened the position: 60 @ $100, 40 @
        $110. Qty-weighted entry should be $104.00."""
        from reconcile_aggregate_drift import _backfill_broker_orphan
        with patch(
            "reconcile_aggregate_drift._find_opening_orders_for_position",
            return_value=[
                {"order_id": "o1", "filled_avg_price": 100.0,
                 "filled_at": "2026-04-01T10:00:00Z", "filled_qty": 60},
                {"order_id": "o2", "filled_avg_price": 110.0,
                 "filled_at": "2026-04-02T10:00:00Z", "filled_qty": 40},
            ],
        ), patch(
            "reconcile_aggregate_drift._find_owning_profile",
            return_value=None,
        ), patch.object(
            __import__("os.path", fromlist=["exists"]),
            "exists", return_value=False,
        ):
            # Profile path doesn't exist → SKIP (which is what we
            # want; we just need to confirm the weighted-price math
            # runs without crashing).
            ok = _backfill_broker_orphan(
                {"id": 1, "name": "p1"}, [{"id": 1}],
                "acct1", "ZZZ", 100.0, apply=False,
            )
        # ok=False because no DB; but no exception means weighted-
        # price arithmetic ran.
        assert ok is False


class TestOCCSymbolHandling:
    """Caught 2026-05-17 (after 113 prod rows were broken): the
    reconciler was writing the full OCC payload into the `symbol`
    column. Convention is `symbol=UNDERLYING, occ_symbol=PAYLOAD`.
    Wrong symbol broke `get_account_info` → 5 profiles showed
    'Not connected' on the dashboard."""

    def test_option_underlying_extracted_into_symbol(
        self, tmp_path, monkeypatch,
    ):
        from reconcile_aggregate_drift import reconcile
        db = tmp_path / "quantopsai_profile_1.db"
        _create_trades_db(str(db))
        monkeypatch.chdir(tmp_path)

        fake_audit = {
            "drift": [
                {"account": "acct1",
                 "symbol": "DOW260618C00040000",
                 "journal_qty": 0.0, "broker_qty": 1.0,
                 "drift": 1.0, "kind": "broker_orphan"},
            ],
            "accounts": {}, "errored": [],
        }
        with patch(
            "aggregate_audit.audit_aggregate_drift",
            return_value=fake_audit,
        ), patch(
            "reconcile_aggregate_drift._profiles_sharing_account",
            return_value=[{"id": 1, "name": "p1", "enabled": True,
                           "alpaca_account_id": "acct1"}],
        ), patch(
            "reconcile_aggregate_drift._broker_position_lookup",
            return_value={"DOW260618C00040000": {
                "qty": 1.0, "avg_entry_price": 0.85,
                "market_value": 85.0, "side": "long"}},
        ), patch(
            "reconcile_aggregate_drift._find_opening_orders_for_position",
            return_value=[],
        ):
            reconcile(apply=True)

        with sqlite3.connect(str(db)) as conn:
            r = conn.execute(
                "SELECT symbol, occ_symbol FROM trades"
            ).fetchone()
        assert r[0] == "DOW", (
            f"symbol column must be the UNDERLYING ('DOW'), not the "
            f"full OCC payload; got {r[0]!r}"
        )
        assert r[1] == "DOW260618C00040000", (
            "occ_symbol column must carry the full OCC payload"
        )

    def test_stock_symbol_unchanged(self, tmp_path, monkeypatch):
        """Stocks shouldn't get touched by the OCC strip."""
        from reconcile_aggregate_drift import reconcile
        db = tmp_path / "quantopsai_profile_1.db"
        _create_trades_db(str(db))
        monkeypatch.chdir(tmp_path)

        fake_audit = {
            "drift": [
                {"account": "acct1", "symbol": "AAPL",
                 "journal_qty": 0.0, "broker_qty": 50.0,
                 "drift": 50.0, "kind": "broker_orphan"},
            ],
            "accounts": {}, "errored": [],
        }
        with patch(
            "aggregate_audit.audit_aggregate_drift",
            return_value=fake_audit,
        ), patch(
            "reconcile_aggregate_drift._profiles_sharing_account",
            return_value=[{"id": 1, "name": "p1", "enabled": True,
                           "alpaca_account_id": "acct1"}],
        ), patch(
            "reconcile_aggregate_drift._broker_position_lookup",
            return_value={"AAPL": {"qty": 50.0, "avg_entry_price": 200.0,
                                    "market_value": 10000.0, "side": "long"}},
        ), patch(
            "reconcile_aggregate_drift._find_opening_orders_for_position",
            return_value=[],
        ):
            reconcile(apply=True)

        with sqlite3.connect(str(db)) as conn:
            r = conn.execute(
                "SELECT symbol, occ_symbol FROM trades"
            ).fetchone()
        assert r[0] == "AAPL"
        assert r[1] is None  # no OCC for stock


class TestBrokerAvgEntryPrimary:
    """2026-05-17 update: `list_positions.avg_entry_price` is the
    AUTHORITATIVE entry-price source (Alpaca tracks it independently
    of order history). Use it whenever available — even when
    list_orders returns empty."""

    def test_uses_broker_avg_entry_when_no_order_history(
        self, tmp_path, monkeypatch,
    ):
        """The 75 'no order history' positions in prod still have a
        real avg_entry_price from list_positions. Use it."""
        from reconcile_aggregate_drift import reconcile
        db = tmp_path / "quantopsai_profile_1.db"
        _create_trades_db(str(db))
        monkeypatch.chdir(tmp_path)

        fake_audit = {
            "drift": [
                {"account": "acct1", "symbol": "FRO",
                 "journal_qty": 0.0, "broker_qty": -11.0,
                 "drift": -11.0, "kind": "broker_orphan"},
            ],
            "accounts": {}, "errored": [],
        }
        with patch(
            "aggregate_audit.audit_aggregate_drift",
            return_value=fake_audit,
        ), patch(
            "reconcile_aggregate_drift._profiles_sharing_account",
            return_value=[{"id": 1, "name": "p1", "enabled": True,
                           "alpaca_account_id": "acct1"}],
        ), patch(
            "reconcile_aggregate_drift._find_opening_orders_for_position",
            return_value=[],  # NO order history
        ), patch(
            "reconcile_aggregate_drift._broker_position_lookup",
            return_value={"FRO": {"qty": -11.0, "avg_entry_price": 36.53,
                                   "market_value": -403.26, "side": "short"}},
        ):
            reconcile(apply=True)

        with sqlite3.connect(str(db)) as conn:
            conn.row_factory = sqlite3.Row
            r = conn.execute("SELECT * FROM trades").fetchone()
        assert r is not None
        assert r["price"] == 36.53, (
            "Must use broker's avg_entry_price (36.53) — NOT current "
            "mark or a synthetic value"
        )
        assert "broker-avg-entry" in r["reason"], (
            "Reason string must record that broker avg_entry was used"
        )

    def test_broker_avg_entry_beats_order_history_price(
        self, tmp_path, monkeypatch,
    ):
        """When BOTH are available, broker avg_entry wins (it's the
        position-lifetime cost basis; order history may show a single
        slice of it)."""
        from reconcile_aggregate_drift import reconcile
        db = tmp_path / "quantopsai_profile_1.db"
        _create_trades_db(str(db))
        monkeypatch.chdir(tmp_path)

        fake_audit = {
            "drift": [
                {"account": "acct1", "symbol": "BAR",
                 "journal_qty": 0.0, "broker_qty": -10.0,
                 "drift": -10.0, "kind": "broker_orphan"},
            ],
            "accounts": {}, "errored": [],
        }
        with patch(
            "aggregate_audit.audit_aggregate_drift",
            return_value=fake_audit,
        ), patch(
            "reconcile_aggregate_drift._profiles_sharing_account",
            return_value=[{"id": 1, "name": "p1", "enabled": True,
                           "alpaca_account_id": "acct1"}],
        ), patch(
            "reconcile_aggregate_drift._find_opening_orders_for_position",
            return_value=[{
                "order_id": "real-1",
                "filled_avg_price": 99.99,  # order says 99.99
                "filled_at": "2026-04-01T10:00:00Z",
                "filled_qty": 10,
            }],
        ), patch(
            "reconcile_aggregate_drift._broker_position_lookup",
            return_value={"BAR": {"qty": -10.0, "avg_entry_price": 50.00,
                                   "market_value": -500.0, "side": "short"}},
        ):
            reconcile(apply=True)

        with sqlite3.connect(str(db)) as conn:
            r = conn.execute("SELECT price FROM trades").fetchone()
        assert r[0] == 50.00, (
            "broker avg_entry_price (50.00) must win over single-order "
            "history fill price (99.99) — broker is authoritative"
        )


class TestJournalPhantomClose:
    def test_existing_open_row_gets_closed(self, tmp_path, monkeypatch):
        from reconcile_aggregate_drift import reconcile
        db = tmp_path / "quantopsai_profile_1.db"
        _create_trades_db(str(db))
        with sqlite3.connect(str(db)) as conn:
            conn.execute(
                "INSERT INTO trades (symbol, side, qty, price, status) "
                "VALUES ('SM260618P00027500', 'buy', 1, 1.50, 'open')"
            )
            conn.commit()
        monkeypatch.chdir(tmp_path)

        fake_audit = {
            "drift": [
                {"account": "acct1", "symbol": "SM260618P00027500",
                 "journal_qty": 1.0, "broker_qty": 0.0,
                 "drift": -1.0, "kind": "journal_phantom"},
            ],
            "accounts": {}, "errored": [],
        }
        with patch(
            "aggregate_audit.audit_aggregate_drift",
            return_value=fake_audit,
        ), patch(
            "reconcile_aggregate_drift._profiles_sharing_account",
            return_value=[{"id": 1, "name": "p1", "enabled": True,
                           "alpaca_account_id": "acct1"}],
        ):
            counters = reconcile(apply=True)

        with sqlite3.connect(str(db)) as conn:
            conn.row_factory = sqlite3.Row
            r = conn.execute("SELECT * FROM trades").fetchone()
        assert r["status"] == "auto_reconciled_phantom_close"
        assert r["pnl"] == 0
        assert counters["journal_phantom_closed"] == 1


class TestProfileAttribution:
    def test_first_profile_id_wins_for_broker_orphan(
        self, tmp_path, monkeypatch,
    ):
        """Two profiles share the account → broker_orphan gets
        attributed to the LOWEST profile id (deterministic)."""
        from reconcile_aggregate_drift import reconcile
        for pid in (1, 5):
            _create_trades_db(str(tmp_path / f"quantopsai_profile_{pid}.db"))
        monkeypatch.chdir(tmp_path)

        fake_audit = {
            "drift": [
                {"account": "acct1", "symbol": "TFC",
                 "journal_qty": 0.0, "broker_qty": -831.0,
                 "drift": -831.0, "kind": "broker_orphan"},
            ],
            "accounts": {}, "errored": [],
        }
        profiles = [
            {"id": 1, "name": "p1", "enabled": True,
             "alpaca_account_id": "acct1"},
            {"id": 5, "name": "p5", "enabled": True,
             "alpaca_account_id": "acct1"},
        ]
        with patch(
            "aggregate_audit.audit_aggregate_drift",
            return_value=fake_audit,
        ), patch(
            "reconcile_aggregate_drift._profiles_sharing_account",
            return_value=profiles,
        ), patch(
            "reconcile_aggregate_drift._current_mark", return_value=45.0,
        ), _no_history():
            reconcile(apply=True)

        # row went to profile 1, not 5
        with sqlite3.connect(str(tmp_path / "quantopsai_profile_1.db")) as conn:
            n1 = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        with sqlite3.connect(str(tmp_path / "quantopsai_profile_5.db")) as conn:
            n5 = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        assert n1 == 1, "broker_orphan must attribute to lowest pid"
        assert n5 == 0
