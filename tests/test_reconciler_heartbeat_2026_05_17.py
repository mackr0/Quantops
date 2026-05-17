"""Tests for the reconciler-heartbeat audit (#170, 2026-05-17).

All five integrity audits are useless if the reconciler isn't
actually running. This audit makes silent cron failure visible.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


def _make_profile_db_with_runs(tmp_path, pid, runs):
    """runs: list of (task_name, started_at_iso). Creates task_runs
    schema and inserts those rows."""
    db = tmp_path / f"quantopsai_profile_{pid}.db"
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE task_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_name TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                duration_seconds REAL,
                status TEXT NOT NULL DEFAULT 'running',
                error_message TEXT
            );
        """)
        conn.executemany(
            "INSERT INTO task_runs (task_name, started_at) VALUES (?, ?)",
            runs,
        )
    return str(db)


def _ctx(pid, db_path):
    return SimpleNamespace(profile_id=pid, db_path=db_path)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────────────
# Heartbeat detection
# ─────────────────────────────────────────────────────────────────────

class TestHeartbeatHealthy:
    def test_recent_reconcile_no_drift(self, tmp_path):
        from integrity_audit import audit_reconciler_heartbeat
        now = datetime.now(tz=timezone.utc)
        db = _make_profile_db_with_runs(tmp_path, 1, [
            ("[P1] Reconcile Trade Statuses",
             _iso(now - timedelta(minutes=5))),
        ])
        with patch(
            "models.build_user_context_from_profile",
            return_value=_ctx(1, db),
        ):
            result = audit_reconciler_heartbeat(1)
        assert result["has_drift"] is False
        assert result["age_minutes"] is not None
        assert result["age_minutes"] < 10

    def test_other_tasks_dont_count_as_reconciler(self, tmp_path):
        """A profile that ran lots of other tasks but never the
        reconciler is stale."""
        from integrity_audit import audit_reconciler_heartbeat
        now = datetime.now(tz=timezone.utc)
        db = _make_profile_db_with_runs(tmp_path, 1, [
            ("[P1] Scan and Trade", _iso(now - timedelta(minutes=2))),
            ("[P1] Crisis Monitor", _iso(now - timedelta(minutes=3))),
        ])
        with patch(
            "models.build_user_context_from_profile",
            return_value=_ctx(1, db),
        ):
            result = audit_reconciler_heartbeat(1)
        assert result["has_drift"] is True


class TestHeartbeatStale:
    def test_old_reconcile_caught(self, tmp_path):
        """Reconciler last ran 2 hours ago → drift."""
        from integrity_audit import audit_reconciler_heartbeat
        now = datetime.now(tz=timezone.utc)
        db = _make_profile_db_with_runs(tmp_path, 1, [
            ("[P1] Reconcile Trade Statuses",
             _iso(now - timedelta(hours=2))),
        ])
        with patch(
            "models.build_user_context_from_profile",
            return_value=_ctx(1, db),
        ):
            result = audit_reconciler_heartbeat(1)
        assert result["has_drift"] is True
        assert result["age_minutes"] > 60

    def test_never_ran_caught(self, tmp_path):
        """task_runs exists but has no reconciler row."""
        from integrity_audit import audit_reconciler_heartbeat
        db = _make_profile_db_with_runs(tmp_path, 1, [])
        with patch(
            "models.build_user_context_from_profile",
            return_value=_ctx(1, db),
        ):
            result = audit_reconciler_heartbeat(1)
        assert result["has_drift"] is True
        assert result["latest_run_at"] is None

    def test_fresh_db_no_task_runs_table_caught(self, tmp_path):
        """A profile DB without the task_runs table is treated as stale
        (it can't prove the reconciler ran)."""
        from integrity_audit import audit_reconciler_heartbeat
        db = tmp_path / "quantopsai_profile_1.db"
        sqlite3.connect(db).close()  # empty schema
        with patch(
            "models.build_user_context_from_profile",
            return_value=_ctx(1, str(db)),
        ):
            result = audit_reconciler_heartbeat(1)
        assert result["has_drift"] is True


class TestHeartbeatBatch:
    def test_batch_sorts_stale_vs_healthy_vs_errored(self, tmp_path):
        from integrity_audit import audit_reconciler_heartbeat_all
        now = datetime.now(tz=timezone.utc)
        db1 = _make_profile_db_with_runs(tmp_path, 1, [
            ("[P1] Reconcile Trade Statuses",
             _iso(now - timedelta(minutes=5))),
        ])
        db2 = _make_profile_db_with_runs(tmp_path, 2, [
            ("[P2] Reconcile Trade Statuses",
             _iso(now - timedelta(hours=3))),
        ])

        def _build(pid):
            if pid == 1:
                return _ctx(1, db1)
            if pid == 2:
                return _ctx(2, db2)
            raise ValueError("nope")

        with patch(
            "models.build_user_context_from_profile", side_effect=_build,
        ):
            result = audit_reconciler_heartbeat_all([1, 2, 3])
        assert len(result["profiles"]) == 3
        assert len(result["drift"]) == 1
        assert result["drift"][0]["profile_id"] == 2
        assert result["errored"] == [3]


# ─────────────────────────────────────────────────────────────────────
# Wiring into audit_runner + issues_collector
# ─────────────────────────────────────────────────────────────────────

class TestAuditRunnerWiring:
    def test_heartbeat_drift_in_audit_runner(self, tmp_path):
        """audit_runner.run_all_audits picks up heartbeat drift."""
        from audit_runner import run_all_audits
        with patch(
            "aggregate_audit.audit_aggregate_drift",
            return_value={"drift": [], "accounts": {}, "errored": []},
        ), patch(
            "aggregate_audit.audit_account_value_parity",
            return_value={"drift": [], "accounts": {}, "errored": []},
        ), patch(
            "aggregate_audit.audit_account_cash_parity",
            return_value={"drift": [], "accounts": {}, "errored": []},
        ), patch(
            "aggregate_audit.audit_account_basis_parity",
            return_value={"drift": [], "accounts": {}, "errored": []},
        ), patch(
            "integrity_audit.audit_equity_identity_all",
            return_value={"profiles": [], "drift": [], "errored": []},
        ), patch(
            "integrity_audit.audit_reconciler_heartbeat_all",
            return_value={
                "profiles": [],
                "drift": [{"profile_id": 5, "latest_run_at": None,
                           "age_minutes": None,
                           "max_age_minutes": 60, "has_drift": True,
                           "errored": None}],
                "errored": [],
            },
        ):
            items = run_all_audits([1, 2, 3, 4, 5])
        types = [it["audit_type"] for it in items]
        assert "reconciler_heartbeat" in types
        # Signature includes profile_id so two different profiles get
        # distinct signatures.
        sig = next(it["signature"] for it in items
                   if it["audit_type"] == "reconciler_heartbeat")
        assert sig == "reconciler_heartbeat:5"

    def test_issues_collector_surfaces_heartbeat(self):
        import issues_collector
        issues_collector._DRIFT_CACHE["ts"] = 0
        with patch(
            "aggregate_audit.audit_aggregate_drift",
            return_value={"drift": [], "accounts": {}, "errored": []},
        ), patch(
            "aggregate_audit.audit_account_value_parity",
            return_value={"drift": [], "accounts": {}, "errored": []},
        ), patch(
            "aggregate_audit.audit_account_cash_parity",
            return_value={"drift": [], "accounts": {}, "errored": []},
        ), patch(
            "aggregate_audit.audit_account_basis_parity",
            return_value={"drift": [], "accounts": {}, "errored": []},
        ), patch(
            "integrity_audit.audit_equity_identity_all",
            return_value={"profiles": [], "drift": [], "errored": []},
        ), patch(
            "integrity_audit.audit_reconciler_heartbeat_all",
            return_value={
                "profiles": [],
                "drift": [{"profile_id": 3, "latest_run_at": None,
                           "age_minutes": 95.2,
                           "max_age_minutes": 60, "has_drift": True,
                           "errored": None}],
                "errored": [],
            },
        ):
            rows, err = issues_collector._collect_aggregate_drift(since_hours=24)
        assert err is None
        hb_rows = [r for r in rows
                   if r["source"].startswith("reconciler_heartbeat")]
        assert len(hb_rows) == 1
        assert hb_rows[0]["level"] == "ERROR"
        assert "stale for profile 3" in hb_rows[0]["message"]
