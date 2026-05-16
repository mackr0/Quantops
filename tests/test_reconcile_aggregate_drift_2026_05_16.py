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

import pytest

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
        ):
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
        ):
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
        ):
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
        ):
            counters = reconcile(apply=True)

        with sqlite3.connect(str(db)) as conn:
            n = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        assert n == 0, (
            "no-mark case must SKIP rather than write a price=0 row"
        )
        assert counters["skipped"] == 1


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
        ):
            reconcile(apply=True)

        # row went to profile 1, not 5
        with sqlite3.connect(str(tmp_path / "quantopsai_profile_1.db")) as conn:
            n1 = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        with sqlite3.connect(str(tmp_path / "quantopsai_profile_5.db")) as conn:
            n5 = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        assert n1 == 1, "broker_orphan must attribute to lowest pid"
        assert n5 == 0
