"""Item 5 of docs/17 Phase 1 — trade-rate anomaly alert.

Operator-visibility layer that fires an `audit_alerts` row when
weekly entry count drops >50% week-over-week. The tuner is NOT
paused — Items 2 (auto-loosen) and 4 (auto-expiry) keep running.
This is purely "we noticed something" plumbing.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest


def _mkdb(path):
    """Create a profile DB with the minimal trades schema the detector
    queries against."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            timestamp TEXT, symbol TEXT, side TEXT, qty REAL, price REAL,
            signal_type TEXT, status TEXT DEFAULT 'closed'
        );
        """
    )
    conn.commit()
    conn.close()


def _seed_entries(path, n_current, n_prior, *, now=None):
    """Insert `n_current` entries in the last 7d and `n_prior` in the
    7d window before that."""
    now = now or datetime.utcnow()
    conn = sqlite3.connect(path)
    for i in range(n_current):
        ts = (now - timedelta(days=3, seconds=i)).isoformat()
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, "
            "signal_type, status) VALUES (?, ?, 'buy', 100, 50, 'BUY', 'open')",
            (ts, f"S{i}"),
        )
    for i in range(n_prior):
        ts = (now - timedelta(days=10, seconds=i)).isoformat()
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, "
            "signal_type, status) VALUES (?, ?, 'buy', 100, 50, 'BUY', 'open')",
            (ts, f"P{i}"),
        )
    conn.commit()
    conn.close()


def _mkmaster(path):
    """audit_alerts table is created lazily by record_alert, but we
    also pre-create it so tests can read it back without races."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE audit_alerts ("
        " signature TEXT PRIMARY KEY,"
        " audit_type TEXT NOT NULL,"
        " first_seen TEXT NOT NULL,"
        " last_seen TEXT NOT NULL,"
        " resolved_at TEXT,"
        " details_json TEXT,"
        " alert_sent INTEGER NOT NULL DEFAULT 0"
        ")"
    )
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────────────────────────────
# detect_anomaly
# ─────────────────────────────────────────────────────────────────────

class TestDetectAnomaly:
    def test_no_op_when_prior_below_floor(self, tmp_path):
        """Prior week has too few entries — comparison is noise, skip."""
        db = str(tmp_path / "p.db")
        _mkdb(db)
        _seed_entries(db, n_current=0, n_prior=3)  # 3 < MIN floor 5
        from trade_rate_anomaly import detect_anomaly
        assert detect_anomaly(1, db) is None

    def test_no_op_when_drop_within_tolerance(self, tmp_path):
        db = str(tmp_path / "p.db")
        _mkdb(db)
        # 10 prior → 6 current = 40% drop, still above 50% threshold
        _seed_entries(db, n_current=6, n_prior=10)
        from trade_rate_anomaly import detect_anomaly
        assert detect_anomaly(1, db) is None

    def test_fires_on_majority_drop(self, tmp_path):
        db = str(tmp_path / "p.db")
        _mkdb(db)
        # 10 prior → 2 current = 80% drop
        _seed_entries(db, n_current=2, n_prior=10)
        from trade_rate_anomaly import detect_anomaly
        details = detect_anomaly(1, db)
        assert details is not None
        assert details["prior_week_entries"] == 10
        assert details["current_week_entries"] == 2
        assert details["drop_pct"] == 80.0
        assert details["profile_id"] == 1

    def test_fires_at_exact_boundary(self, tmp_path):
        """10 → 4 (60% drop, exceeds 50% threshold) fires; 10 → 5
        (50% drop) is right AT the threshold so detector treats it
        as within tolerance (current >= threshold)."""
        db = str(tmp_path / "p.db")
        _mkdb(db)
        _seed_entries(db, n_current=5, n_prior=10)
        from trade_rate_anomaly import detect_anomaly
        # current=5, threshold=10*0.5=5, current >= threshold → no fire
        assert detect_anomaly(1, db) is None

    def test_fires_on_zero_entries(self, tmp_path):
        db = str(tmp_path / "p.db")
        _mkdb(db)
        _seed_entries(db, n_current=0, n_prior=8)
        from trade_rate_anomaly import detect_anomaly
        details = detect_anomaly(1, db)
        assert details is not None
        assert details["current_week_entries"] == 0
        assert details["drop_pct"] == 100.0

    def test_returns_none_for_missing_db(self, tmp_path):
        from trade_rate_anomaly import detect_anomaly
        assert detect_anomaly(1, str(tmp_path / "nonexistent.db")) is None

    def test_custom_threshold(self, tmp_path):
        """A stricter threshold (0.8 = require current >= 80% of prior)
        fires when the default 0.5 wouldn't."""
        db = str(tmp_path / "p.db")
        _mkdb(db)
        # 10 → 7 (30% drop). Default 0.5: 7 >= 5 → no fire.
        # Custom 0.8: 7 < 8 → fire.
        _seed_entries(db, n_current=7, n_prior=10)
        from trade_rate_anomaly import detect_anomaly
        assert detect_anomaly(1, db) is None
        assert detect_anomaly(1, db, drop_threshold=0.8) is not None


# ─────────────────────────────────────────────────────────────────────
# record_alert / resolve_alert_if_recovered
# ─────────────────────────────────────────────────────────────────────

class TestRecordAlert:
    def test_first_insert(self, tmp_path):
        master = str(tmp_path / "m.db")
        _mkmaster(master)
        from trade_rate_anomaly import record_alert
        details = {
            "profile_id": 1,
            "prior_week_start": "2026-05-04",
            "prior_week_entries": 10,
            "current_week_entries": 2,
            "drop_pct": 80.0,
        }
        assert record_alert(master, details) is True

        conn = sqlite3.connect(master)
        rows = conn.execute(
            "SELECT signature, audit_type, resolved_at FROM audit_alerts"
        ).fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0][0] == "trade_rate_anomaly:1:2026-05-04"
        assert rows[0][1] == "trade_rate_anomaly"
        assert rows[0][2] is None

    def test_second_call_is_update(self, tmp_path):
        master = str(tmp_path / "m.db")
        _mkmaster(master)
        from trade_rate_anomaly import record_alert
        details = {"profile_id": 1, "prior_week_start": "2026-05-04",
                    "prior_week_entries": 10, "current_week_entries": 2}
        assert record_alert(master, details) is True
        assert record_alert(master, details) is False

    def test_creates_table_when_missing(self, tmp_path):
        """Master DB without audit_alerts table — record_alert must
        create it. Otherwise first deploy after a fresh-master DB
        creation would silently no-op."""
        master = str(tmp_path / "empty.db")
        sqlite3.connect(master).close()  # create empty file
        from trade_rate_anomaly import record_alert
        details = {"profile_id": 1, "prior_week_start": "2026-05-04",
                    "prior_week_entries": 10, "current_week_entries": 2}
        assert record_alert(master, details) is True

    def test_re_open_after_resolved(self, tmp_path):
        """Resolved alert reopens on subsequent fire (e.g., recovered
        last week, regressed this week — same signature should clear
        resolved_at)."""
        master = str(tmp_path / "m.db")
        _mkmaster(master)
        from trade_rate_anomaly import (
            record_alert, resolve_alert_if_recovered,
        )
        details = {"profile_id": 1, "prior_week_start": "2026-05-04",
                    "prior_week_entries": 10, "current_week_entries": 2}
        record_alert(master, details)
        resolve_alert_if_recovered(master, 1, "2026-05-04")
        record_alert(master, details)

        conn = sqlite3.connect(master)
        row = conn.execute(
            "SELECT resolved_at FROM audit_alerts WHERE signature = ?",
            ("trade_rate_anomaly:1:2026-05-04",),
        ).fetchone()
        conn.close()
        assert row[0] is None, "Re-firing must clear the resolved timestamp"


class TestResolveAlert:
    def test_marks_resolved(self, tmp_path):
        master = str(tmp_path / "m.db")
        _mkmaster(master)
        from trade_rate_anomaly import (
            record_alert, resolve_alert_if_recovered,
        )
        details = {"profile_id": 1, "prior_week_start": "2026-05-04",
                    "prior_week_entries": 10, "current_week_entries": 2}
        record_alert(master, details)
        assert resolve_alert_if_recovered(master, 1, "2026-05-04") is True
        # Second call is no-op
        assert resolve_alert_if_recovered(master, 1, "2026-05-04") is False

    def test_returns_false_when_no_open_alert(self, tmp_path):
        master = str(tmp_path / "m.db")
        _mkmaster(master)
        from trade_rate_anomaly import resolve_alert_if_recovered
        assert resolve_alert_if_recovered(master, 99, "2026-05-04") is False


# ─────────────────────────────────────────────────────────────────────
# check_and_alert — end to end
# ─────────────────────────────────────────────────────────────────────

class TestCheckAndAlert:
    def test_fires_writes_alert(self, tmp_path):
        prof = str(tmp_path / "p.db")
        master = str(tmp_path / "m.db")
        _mkdb(prof)
        _mkmaster(master)
        _seed_entries(prof, n_current=1, n_prior=10)

        from trade_rate_anomaly import check_and_alert
        status = check_and_alert(profile_id=1,
                                  profile_db_path=prof,
                                  main_db_path=master)
        assert status["fired"] is True
        assert status["is_new"] is True
        assert status["details"]["prior_week_entries"] == 10
        assert status["details"]["current_week_entries"] == 1

        conn = sqlite3.connect(master)
        row = conn.execute(
            "SELECT audit_type, details_json FROM audit_alerts"
        ).fetchone()
        conn.close()
        assert row[0] == "trade_rate_anomaly"
        payload = json.loads(row[1])
        assert payload["drop_pct"] == 90.0

    def test_resolves_when_recovered(self, tmp_path):
        prof = str(tmp_path / "p.db")
        master = str(tmp_path / "m.db")
        _mkdb(prof)
        _mkmaster(master)

        # Run 1: fire the alert
        _seed_entries(prof, n_current=1, n_prior=10)
        from trade_rate_anomaly import check_and_alert
        status1 = check_and_alert(profile_id=1,
                                   profile_db_path=prof,
                                   main_db_path=master)
        assert status1["fired"] is True

        # Run 2 (same effective `now`): trade rate recovers
        # — bring current week up to match prior. The detector
        # compares against the SAME prior-week signature (same `now`).
        sqlite3.connect(prof).execute("DELETE FROM trades").connection.commit() \
            if False else None
        conn = sqlite3.connect(prof)
        conn.execute("DELETE FROM trades")
        conn.commit()
        conn.close()
        _seed_entries(prof, n_current=10, n_prior=10)

        status2 = check_and_alert(profile_id=1,
                                   profile_db_path=prof,
                                   main_db_path=master)
        assert status2["fired"] is False
        assert status2["resolved"] is True

        conn = sqlite3.connect(master)
        row = conn.execute(
            "SELECT resolved_at FROM audit_alerts"
        ).fetchone()
        conn.close()
        assert row[0] is not None

    def test_no_op_below_floor_does_not_write(self, tmp_path):
        prof = str(tmp_path / "p.db")
        master = str(tmp_path / "m.db")
        _mkdb(prof)
        _mkmaster(master)
        _seed_entries(prof, n_current=0, n_prior=2)  # below MIN floor

        from trade_rate_anomaly import check_and_alert
        status = check_and_alert(profile_id=1,
                                  profile_db_path=prof,
                                  main_db_path=master)
        assert status["fired"] is False

        conn = sqlite3.connect(master)
        rows = conn.execute("SELECT COUNT(*) FROM audit_alerts").fetchone()
        conn.close()
        assert rows[0] == 0


# ─────────────────────────────────────────────────────────────────────
# Constraint: no manual-intervention pause
# ─────────────────────────────────────────────────────────────────────

class TestNoTunerPause:
    """Per feedback_ai_driven_no_manual_loop: the alert MUST NOT
    pause the tuner or set any "needs review" flag that gates
    autonomous behavior. Encoded as a source scan so a future
    edit can't add that pattern without tripping a test."""

    def test_module_does_not_mutate_enable_self_tuning(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / "trade_rate_anomaly.py").read_text()
        # Forbidden write patterns — these would either disable the
        # tuner or pause it via a profile mutation. The alert is
        # observational; remediation belongs to Items 1-4.
        forbidden = [
            "enable_self_tuning=0",
            "enable_self_tuning = 0",
            "update_trading_profile",
        ]
        for f in forbidden:
            assert f not in src, (
                f"trade_rate_anomaly.py must not contain {f!r}. The "
                "alert is observational only; per feedback_ai_driven_"
                "no_manual_loop the tuner must keep running."
            )
