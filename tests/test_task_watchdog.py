"""Tests for task_watchdog — run-completion tracking + stall detection."""

from __future__ import annotations

import sqlite3
import time
import pytest


class TestTrackRun:
    def test_success_path_marks_completed(self, tmp_profile_db):
        from task_watchdog import track_run
        with track_run(tmp_profile_db, "test_task"):
            time.sleep(0.01)

        conn = sqlite3.connect(tmp_profile_db)
        row = conn.execute(
            "SELECT task_name, status, duration_seconds, completed_at "
            "FROM task_runs WHERE task_name='test_task'"
        ).fetchone()
        conn.close()
        assert row[0] == "test_task"
        assert row[1] == "completed"
        assert row[2] > 0      # duration recorded
        assert row[3] is not None   # completed_at set

    def test_exception_marks_failed_and_reraises(self, tmp_profile_db):
        from task_watchdog import track_run
        with pytest.raises(ValueError, match="boom"):
            with track_run(tmp_profile_db, "test_fail"):
                raise ValueError("boom")

        conn = sqlite3.connect(tmp_profile_db)
        row = conn.execute(
            "SELECT status, error_message FROM task_runs "
            "WHERE task_name='test_fail'"
        ).fetchone()
        conn.close()
        assert row[0] == "failed"
        assert "boom" in row[1]

    def test_silent_on_missing_table(self, tmp_path):
        """If the DB doesn't have the table yet, track_run is a no-op."""
        from task_watchdog import track_run
        empty = str(tmp_path / "empty.db")
        sqlite3.connect(empty).close()
        with track_run(empty, "test"):
            pass  # must not raise


class TestCheckStalledRuns:
    def test_recent_run_not_stalled(self, tmp_profile_db):
        from task_watchdog import track_run, check_stalled_runs
        with track_run(tmp_profile_db, "fresh_task"):
            pass
        stalled = check_stalled_runs(tmp_profile_db, stall_minutes=30)
        assert stalled == []

    def test_old_running_row_is_stalled(self, tmp_profile_db):
        """Seed an ancient 'running' row and verify watchdog catches it."""
        conn = sqlite3.connect(tmp_profile_db)
        conn.execute(
            "INSERT INTO task_runs (task_name, started_at, status) "
            "VALUES ('hung_task', datetime('now', '-45 minutes'), 'running')"
        )
        conn.commit()
        conn.close()

        from task_watchdog import check_stalled_runs
        stalled = check_stalled_runs(tmp_profile_db, stall_minutes=30)
        assert len(stalled) == 1
        assert stalled[0]["task_name"] == "hung_task"
        assert stalled[0]["minutes_elapsed"] >= 30

    def test_stalled_row_marked_and_not_realerted(self, tmp_profile_db):
        """Once a row is marked 'stalled', subsequent scans shouldn't
        return it — so we don't re-alert on every watchdog tick."""
        conn = sqlite3.connect(tmp_profile_db)
        conn.execute(
            "INSERT INTO task_runs (task_name, started_at, status) "
            "VALUES ('stuck', datetime('now', '-60 minutes'), 'running')"
        )
        conn.commit()
        conn.close()

        from task_watchdog import check_stalled_runs
        first = check_stalled_runs(tmp_profile_db, stall_minutes=30)
        assert len(first) == 1
        # Status should now be 'stalled' in the DB
        conn = sqlite3.connect(tmp_profile_db)
        status = conn.execute(
            "SELECT status FROM task_runs WHERE task_name='stuck'"
        ).fetchone()[0]
        conn.close()
        assert status == "stalled"
        # Second scan: no new rows (already marked stalled)
        second = check_stalled_runs(tmp_profile_db, stall_minutes=30)
        assert second == []


class TestSummary:
    def test_summary_counts_by_status(self, tmp_profile_db):
        from task_watchdog import track_run, summary
        # 3 completed, 1 failed
        for i in range(3):
            with track_run(tmp_profile_db, f"ok_{i}"):
                pass
        try:
            with track_run(tmp_profile_db, "bad"):
                raise RuntimeError("x")
        except RuntimeError:
            pass

        s = summary(tmp_profile_db, hours=1)
        assert s["completed"] == 3
        assert s["failed"] == 1
        assert s["running"] == 0
        assert s["stalled"] == 0
