"""Backtester dashboard panel — three layers shipped 2026-06-07:

  1. Smoke test on the existing per-profile flow (button →
     POST /api/backtest/<pid> → GET /api/backtest/status/<job_id>
     → render results). The button + JS had been wired for months
     but had no Flask-test-client coverage; per the standing rule
     `feedback_ui_buttons_must_have_smoke_tests.md`, code that
     compiles ≠ button that works.

  2. Persistent backtest history (`backtest_history` table + new
     `/backtest-history` route + nav link). Previously, results
     vanished from `/tmp/quantopsai_backtest_jobs.json` after the
     30-min expiry; now every run is recorded forever in the
     master DB with current/proposed params + outcome.

  3. Nav-bar link to `/backtest-history` so the page is
     discoverable from every authenticated view.

This file pins all three layers structurally + behaviorally so a
future refactor can't silently break the operator-visible chain.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def client(tmp_main_db):
    import config
    config.DB_PATH = tmp_main_db
    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        yield c


@pytest.fixture
def logged_in_with_profile(client, tmp_main_db):
    import config
    config.DB_PATH = tmp_main_db
    from models import create_user, create_trading_profile
    create_user("test@test.com", "password123", "Test", is_admin=True)
    client.post("/login", data={
        "email": "test@test.com",
        "password": "password123",
    }, follow_redirects=True)
    with closing(sqlite3.connect(tmp_main_db)) as conn:
        uid = conn.execute(
            "SELECT id FROM users WHERE email='test@test.com'"
        ).fetchone()[0]
    pid = create_trading_profile(uid, "Backtest Test", "stocks")
    return client, uid, pid


# ---------------------------------------------------------------------------
# Layer 1 — per-profile backtest API smoke test
# ---------------------------------------------------------------------------

class TestPerProfileBacktestAPI:

    def test_settings_page_renders_backtest_button(
            self, logged_in_with_profile,
    ):
        """The Settings page MUST render the 'Backtest These Settings'
        button. Without this, the entire feature is invisible."""
        client, _uid, _pid = logged_in_with_profile
        resp = client.get("/settings")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8", errors="ignore")
        assert "Backtest These Settings" in body, (
            "Settings page lost the 'Backtest These Settings' button. "
            "The per-profile backtest flow is invisible without it."
        )
        assert 'class="outline backtest-btn"' in body, (
            "The .backtest-btn class is the JS handler hook in "
            "settings.html. If it disappears the button does nothing."
        )

    def test_api_backtest_post_returns_job_id(
            self, logged_in_with_profile, monkeypatch,
    ):
        """POST /api/backtest/<pid> returns {job_id: '...'} synchronously.
        The actual backtest runs in a thread; the API call is just
        the kickoff."""
        client, _uid, pid = logged_in_with_profile
        # Stub the worker so the test doesn't actually run a backtest
        captured = {}
        def _fake_start(*args, **kwargs):
            captured.update(kwargs)
            captured["args"] = args
            return "fake-job-1"
        monkeypatch.setattr(
            "backtest_worker.start_backtest", _fake_start,
        )
        resp = client.post(
            f"/api/backtest/{pid}",
            json={"stop_loss_pct": 0.05, "take_profit_pct": 0.12},
        )
        assert resp.status_code == 200, resp.data[:200]
        payload = resp.get_json()
        assert payload.get("job_id") == "fake-job-1"
        # 2026-06-07 — the API MUST pass profile_id + user_id to the
        # worker so backtest_history can attribute the run. Pin the
        # call signature so a refactor can't drop them silently.
        assert captured.get("profile_id") == pid, (
            "/api/backtest must pass profile_id to start_backtest — "
            "without it, backtest_history rows are unattributable"
        )
        assert captured.get("user_id") is not None, (
            "/api/backtest must pass user_id to start_backtest so "
            "the history page can filter by current_user"
        )

    def test_api_backtest_status_returns_expected_shape(
            self, logged_in_with_profile, monkeypatch,
    ):
        """GET /api/backtest/status/<job_id> returns a dict with the
        contract the polling JS depends on."""
        client, _uid, _pid = logged_in_with_profile
        monkeypatch.setattr(
            "backtest_worker.get_job_status",
            lambda jid: {
                "status": "complete",
                "result": {"current": {"total_return_pct": 5.0},
                           "proposed": {"total_return_pct": 7.5}},
                "error": None,
                "progress": "done",
                "elapsed_seconds": 12,
                "changes": ["stop_loss_pct: 3.0% → 5.0%"],
            },
        )
        resp = client.get("/api/backtest/status/fake-job-1")
        assert resp.status_code == 200
        payload = resp.get_json()
        # These keys are what the JS in settings.html reads (line
        # 1318-1329 of templates/settings.html). Pin them so a
        # rename breaks the test, not the live UI.
        for key in ("status", "result", "progress", "elapsed_seconds",
                     "changes"):
            assert key in payload, (
                f"/api/backtest/status response is missing {key!r}; "
                f"the polling JS will silently fail"
            )


# ---------------------------------------------------------------------------
# Layer 2 — persistent backtest history
# ---------------------------------------------------------------------------

class TestBacktestHistoryPersistence:

    def test_persist_history_row_on_start(self, tmp_main_db, monkeypatch):
        """A backtest job started via the worker MUST insert a
        running-status row in backtest_history immediately, so an
        operator inspecting the page mid-job sees the run."""
        import config
        config.DB_PATH = tmp_main_db
        # Stub the threaded run so we only test the persist side
        monkeypatch.setattr(
            "backtest_worker._cleanup_old_jobs", lambda: None,
        )
        # Don't actually fire the thread; just call _persist_history_row
        from backtest_worker import _persist_history_row
        _persist_history_row(
            job_id="job-A1", profile_id=42, user_id=1,
            market_type="stocks", status="running",
            current_params={"stop_loss_pct": 0.03},
            proposed_params={"stop_loss_pct": 0.05},
            changes=["stop_loss_pct: 3.0% → 5.0%"],
        )
        with closing(sqlite3.connect(tmp_main_db)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM backtest_history WHERE job_id='job-A1'"
            ).fetchone()
        assert row is not None, (
            "Starting a job must insert a backtest_history row. "
            "Without it the history page misses in-flight jobs."
        )
        assert row["status"] == "running"
        assert row["profile_id"] == 42
        assert row["user_id"] == 1
        assert row["market_type"] == "stocks"
        assert json.loads(row["current_params_json"]) == {"stop_loss_pct": 0.03}
        assert json.loads(row["proposed_params_json"]) == {"stop_loss_pct": 0.05}
        assert json.loads(row["changes_json"]) == [
            "stop_loss_pct: 3.0% → 5.0%",
        ]

    def test_finalize_updates_row_on_complete(self, tmp_main_db):
        import config
        config.DB_PATH = tmp_main_db
        from backtest_worker import (
            _persist_history_row, _finalize_history_row,
        )
        _persist_history_row(
            job_id="job-B1", profile_id=1, user_id=1,
            market_type="stocks", status="running",
            current_params={"a": 1}, proposed_params={"a": 2},
            changes=[],
        )
        _finalize_history_row(
            job_id="job-B1", status="complete",
            result={"current": {"total_return_pct": 5.0},
                    "proposed": {"total_return_pct": 7.5}},
            error=None,
        )
        with closing(sqlite3.connect(tmp_main_db)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT status, completed_at, result_json, error "
                "FROM backtest_history WHERE job_id='job-B1'"
            ).fetchone()
        assert row["status"] == "complete"
        assert row["completed_at"] is not None, (
            "Completed_at must be stamped on finalize so the history "
            "page can order by it"
        )
        result = json.loads(row["result_json"])
        assert result["proposed"]["total_return_pct"] == 7.5
        assert row["error"] is None

    def test_finalize_records_error_on_failed_job(self, tmp_main_db):
        import config
        config.DB_PATH = tmp_main_db
        from backtest_worker import (
            _persist_history_row, _finalize_history_row,
        )
        _persist_history_row(
            job_id="job-C1", profile_id=1, user_id=1,
            market_type="stocks", status="running",
            current_params={}, proposed_params={}, changes=[],
        )
        _finalize_history_row(
            job_id="job-C1", status="failed",
            result=None, error="Downloaded zero bars",
        )
        with closing(sqlite3.connect(tmp_main_db)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT status, error FROM backtest_history "
                "WHERE job_id='job-C1'"
            ).fetchone()
        assert row["status"] == "failed"
        assert row["error"] == "Downloaded zero bars"


class TestBacktestHistoryRoute:

    def test_history_page_renders_empty_state(self, logged_in_with_profile):
        """No backtests yet → friendly empty state, not a crash."""
        client, _, _ = logged_in_with_profile
        resp = client.get("/backtest-history")
        assert resp.status_code == 200, resp.data[:300]
        body = resp.data.decode("utf-8", errors="ignore")
        assert "No backtests recorded yet" in body

    def test_history_page_renders_completed_run(
            self, logged_in_with_profile, tmp_main_db,
    ):
        """A row in backtest_history must render on the page with
        the current/proposed returns and the delta."""
        client, uid, pid = logged_in_with_profile
        import config
        config.DB_PATH = tmp_main_db
        from backtest_worker import (
            _persist_history_row, _finalize_history_row,
        )
        _persist_history_row(
            job_id="job-X1", profile_id=pid, user_id=uid,
            market_type="stocks", status="running",
            current_params={"stop_loss_pct": 0.03},
            proposed_params={"stop_loss_pct": 0.05},
            changes=["stop_loss_pct: 3.0% → 5.0%"],
        )
        _finalize_history_row(
            job_id="job-X1", status="complete",
            result={"current": {"total_return_pct": 4.2},
                    "proposed": {"total_return_pct": 6.8}},
            error=None,
        )
        resp = client.get("/backtest-history")
        body = resp.data.decode("utf-8", errors="ignore")
        assert "job-X1" in body
        assert "Backtest Test" in body  # profile name from fixture
        assert "+4.20%" in body or "+4.2%" in body, (
            "Current return missing from page"
        )
        assert "+6.80%" in body or "+6.8%" in body
        # Delta = 6.80 - 4.20 = +2.60
        assert "+2.60%" in body or "+2.6%" in body

    def test_history_page_filters_by_current_user(
            self, logged_in_with_profile, tmp_main_db,
    ):
        """A row owned by a different user_id must NOT appear on
        this user's history page."""
        client, uid, pid = logged_in_with_profile
        import config
        config.DB_PATH = tmp_main_db
        from backtest_worker import (
            _persist_history_row, _finalize_history_row,
        )
        _persist_history_row(
            job_id="job-mine", profile_id=pid, user_id=uid,
            market_type="stocks", status="running",
            current_params={}, proposed_params={}, changes=[],
        )
        _finalize_history_row(
            job_id="job-mine", status="complete",
            result={"current": {"total_return_pct": 1.0},
                    "proposed": {"total_return_pct": 1.0}},
            error=None,
        )
        # Different user's run
        _persist_history_row(
            job_id="job-theirs", profile_id=999, user_id=9999,
            market_type="stocks", status="running",
            current_params={}, proposed_params={}, changes=[],
        )
        _finalize_history_row(
            job_id="job-theirs", status="complete",
            result={"current": {"total_return_pct": 1.0},
                    "proposed": {"total_return_pct": 1.0}},
            error=None,
        )
        resp = client.get("/backtest-history")
        body = resp.data.decode("utf-8", errors="ignore")
        assert "job-mine" in body
        assert "job-theirs" not in body, (
            "Backtest history must filter by current_user.id; without "
            "this, every user sees every other user's runs"
        )


# ---------------------------------------------------------------------------
# Layer 3 — nav-bar link
# ---------------------------------------------------------------------------

class TestNavbarLink:

    def test_dashboard_renders_backtests_nav_link(
            self, logged_in_with_profile,
    ):
        """The 'Backtests' nav link must appear in the top nav on
        the dashboard (and every authenticated page since it's in
        base.html). Without this the history page is undiscoverable."""
        client, _, _ = logged_in_with_profile
        resp = client.get("/dashboard")
        body = resp.data.decode("utf-8", errors="ignore")
        assert 'href="/backtest-history"' in body, (
            "base.html nav lost the /backtest-history link; "
            "the history page becomes invisible to operators"
        )

    def test_settings_renders_backtests_nav_link(
            self, logged_in_with_profile,
    ):
        client, _, _ = logged_in_with_profile
        resp = client.get("/settings")
        body = resp.data.decode("utf-8", errors="ignore")
        assert 'href="/backtest-history"' in body
