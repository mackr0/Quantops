"""Structural guardrail: stalled-task alerts must reflect REAL state.

The bug class (2026-05-15).
The previous watchdog had two related lies operators were seeing:

  1. False-positive stall alerts after every deploy. A scheduler
     restart killed in-flight task_runs rows; the next watchdog
     pass found them with status='running' and reported "stalled —
     likely slow API responses from Alpaca or the AI provider." The
     task hadn't been hung at all; the process had been killed.

  2. The diagnosis text on TRUE stalls was a hard-coded if/elif on
     task name + elapsed time. It fabricated culprits ("likely
     Alpaca slow", "likely a hung price fetch") with zero evidence
     from the actual system state. Operators learned to ignore the
     diagnosis line, which defeated its purpose.

The structural fix:
  - `mark_orphaned_at_startup()` runs at scheduler boot and bulk-
    converts every still-`running` row to `status='orphaned_restart'`
    BEFORE the watchdog gets a chance to mis-diagnose them.
  - `diagnose_stalled_run()` reads ai_cost_ledger, activity_log, and
    ai_predictions to report what the task was actually doing. If
    no evidence is available it says so plainly — no fabricated
    culprits.

This test pins both contracts:
  - Restart-orphaned rows are taken off the stalled-detection path
  - True stalls get evidence-based diagnoses, not fabricated ones
  - The diagnosis function never invents a culprit when no evidence
    is present
"""
from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


@pytest.fixture
def profile_db(tmp_path):
    """Per-profile DB with the schema task_runs / ai_cost_ledger /
    activity_log / ai_predictions need to exist on. Uses the journal
    init path so the structure matches production."""
    db = str(tmp_path / "quantopsai_profile_test.db")
    from journal import init_db
    init_db(db)
    return db


def _seed_running_row(db, task_name, age_minutes):
    """Insert a task_runs row mid-flight (status='running', no
    completed_at) with a configurable age. Returns the row id."""
    with closing(sqlite3.connect(db)) as conn:
        cur = conn.execute(
            "INSERT INTO task_runs (task_name, started_at, status) "
            "VALUES (?, datetime('now', ?), 'running')",
            (task_name, f"-{int(age_minutes)} minutes"),
        )
        conn.commit()
        return cur.lastrowid


class TestMarkOrphanedAtStartup:
    def test_running_rows_become_orphaned_restart(self, profile_db):
        """Headline contract: every status='running' row at startup
        becomes status='orphaned_restart'. They are by definition
        zombies — the parent process that wrote them is gone."""
        _seed_running_row(profile_db, "[Mid Cap] Scan & Trade", 45)
        _seed_running_row(profile_db, "[Mid Cap] Check Exits", 12)

        from task_watchdog import mark_orphaned_at_startup
        orphaned = mark_orphaned_at_startup(profile_db)
        assert len(orphaned) == 2

        with closing(sqlite3.connect(profile_db)) as conn:
            rows = conn.execute(
                "SELECT status, error_message FROM task_runs",
            ).fetchall()
        for status, err in rows:
            assert status == "orphaned_restart"
            assert "restart" in (err or "").lower()

    def test_completed_rows_unaffected(self, profile_db):
        """Cleanup must not touch already-completed rows. If we
        wiped completed rows we'd lose the duration history the
        diagnosis logic relies on."""
        with closing(sqlite3.connect(profile_db)) as conn:
            conn.execute(
                "INSERT INTO task_runs (task_name, started_at, "
                "completed_at, status, duration_seconds) "
                "VALUES ('[Mid Cap] Scan & Trade', "
                "datetime('now','-2 hours'), datetime('now','-119 minutes'), "
                "'completed', 60.0)",
            )
            conn.commit()

        from task_watchdog import mark_orphaned_at_startup
        orphaned = mark_orphaned_at_startup(profile_db)
        assert orphaned == []

        with closing(sqlite3.connect(profile_db)) as conn:
            status = conn.execute(
                "SELECT status FROM task_runs LIMIT 1",
            ).fetchone()[0]
        assert status == "completed"

    def test_orphaned_rows_do_not_reach_check_stalled(self, profile_db):
        """The whole point: after marking orphans at startup, the
        next `check_stalled_runs` pass returns empty. Operators stop
        seeing the false-positive 'stalled' alerts."""
        _seed_running_row(profile_db, "[Large Cap] Scan & Trade", 60)
        _seed_running_row(profile_db, "[Small Cap] Resolve Predictions", 90)

        from task_watchdog import (
            mark_orphaned_at_startup, check_stalled_runs,
        )
        mark_orphaned_at_startup(profile_db)
        stalled = check_stalled_runs(profile_db, stall_minutes=30)
        assert stalled == [], (
            f"Orphaned-restart rows leaked into the stall path: "
            f"{[r['task_name'] for r in stalled]}"
        )


class TestDiagnoseStalledRunUsesRealEvidence:
    def test_reports_recent_ai_call_when_present(self, profile_db):
        """If the AI was responding within the stall window, the
        diagnosis must say so — that rules out 'AI provider hang'
        as the cause and points the operator at the next
        suspicious step."""
        with closing(sqlite3.connect(profile_db)) as conn:
            conn.execute(
                "INSERT INTO ai_cost_ledger "
                "(timestamp, provider, model, input_tokens, "
                " output_tokens, purpose, estimated_cost_usd) "
                "VALUES (datetime('now','-5 minutes'), 'anthropic', "
                "'claude-haiku-4-5-20251001', 100, 50, "
                "'batch_select', 0.0003)",
            )
            conn.commit()

        from task_watchdog import diagnose_stalled_run
        diag = diagnose_stalled_run(
            profile_db, "[Mid Cap] Scan & Trade",
            "2026-05-15 18:00:00", 45,
        )
        assert "AI was responding" in diag
        assert "batch_select" in diag

    def test_says_no_evidence_when_silent_db(self, profile_db):
        """Empty profile DB → diagnose must report concrete negative
        findings ('no AI calls completed since task started') NOT
        fabricated culprits. The structural promise is: every claim
        in the output is backed by a real (or really-empty) row in a
        real table. This test catches any future 'default to blame
        Alpaca' regression — no `likely …` text without evidence."""
        from task_watchdog import diagnose_stalled_run
        diag = diagnose_stalled_run(
            profile_db, "[Mid Cap] Scan & Trade",
            "2026-05-15 18:00:00", 45,
        )
        # The output must reflect the REAL state — empty tables
        # produce a "no … since" finding, NOT an invented cause.
        assert (
            "no AI calls completed" in diag
            or "cause indeterminate" in diag
        ), f"Expected evidence-based negative finding; got: {diag}"
        for fabricated in (
            "likely slow API",
            "likely a hung",
            "likely a price fetch",
            "likely a slow",
            "Alpaca",  # never name a vendor without evidence
        ):
            assert fabricated not in diag, (
                f"Fabricated culprit '{fabricated}' leaked into "
                f"diagnose_stalled_run output: {diag}"
            )

    def test_includes_last_activity_log_entry(self, profile_db):
        """Last activity_log entry shows what the task was last
        observed doing — concrete signal beats any fabricated
        guess."""
        # activity_log lives in the master DB but it can also exist
        # in the profile DB shape for tests where journal.init_db
        # creates a parallel table. Since this test is purely about
        # the diagnose function reading the table when present,
        # create the table inline.
        with closing(sqlite3.connect(profile_db)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS activity_log "
                "(id INTEGER PRIMARY KEY AUTOINCREMENT, "
                " profile_id INTEGER, user_id INTEGER, "
                " timestamp TEXT, activity_type TEXT, "
                " title TEXT, detail TEXT, symbol TEXT)",
            )
            conn.execute(
                "INSERT INTO activity_log (timestamp, activity_type, "
                "title) VALUES (datetime('now','-3 minutes'), "
                "'specialist_ensemble', 'Specialist ensemble: 4 calls')",
            )
            conn.commit()

        from task_watchdog import diagnose_stalled_run
        diag = diagnose_stalled_run(
            profile_db, "[Mid Cap] Scan & Trade",
            "2026-05-15 18:00:00", 45,
        )
        assert "Specialist ensemble" in diag


class TestNoFabricatedCulpritsAnywhere:
    """Class-level guard: the production multi_scheduler must NOT
    contain any of the previously-fabricated culprit strings. If a
    future change re-introduces "likely slow API responses" or any
    of the other invented causes, this test catches it before
    deploy."""

    def test_multi_scheduler_has_no_fabricated_culprit_text(self):
        import inspect
        import multi_scheduler

        src = inspect.getsource(multi_scheduler)
        forbidden = [
            "likely slow API responses",
            "likely a price fetch timeout",
            "likely a slow position/account fetch",
            "likely a hung API call",
        ]
        offenders = [s for s in forbidden if s in src]
        assert not offenders, (
            f"Fabricated-culprit strings found in multi_scheduler.py: "
            f"{offenders}. Diagnoses must come from "
            f"diagnose_stalled_run (which reads real evidence), not "
            f"from hard-coded if/elif text."
        )
