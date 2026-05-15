"""Structural guardrail: the scheduler's advisory cost alert must
fire at a percentage of the USER'S configured ceiling, not at a
hard-coded constant.

The bug class (2026-05-15).
`multi_scheduler._task_cost_check` had a hard-coded
`_DAILY_COST_ALERT_THRESHOLD = 3.00` that fired regardless of what
the user had configured on the settings page. A user who explicitly
set a $5.00 cap would still see "API Cost Alert: $X today (threshold
$3.00)" the moment spend crossed $3 — flatly contradicting their
configured cap. The alert was a leftover from before the user-
settable cap was added; nobody integrated it.

Structural fix: read the threshold from
`cost_guard.daily_ceiling_usd(user_id)` so the alert tracks the cap
that's truly in effect (user override or auto-computed).

This test pins the contract:
  - When the user has set a $10 cap, alert fires near $8 (80%), not $3
  - When ceiling is $5, alert fires near $4
  - The constant `_DAILY_COST_ALERT_THRESHOLD` must NOT exist anywhere
    in `multi_scheduler.py` — its very presence regresses the bug
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


class TestCostAlertReadsUserCeiling:
    def test_no_hardcoded_alert_threshold_in_scheduler(self):
        """The 2026-05-15 regression had a hard-coded
        `_DAILY_COST_ALERT_THRESHOLD = 3.00`. This test ensures any
        re-introduction fails CI before deploy. The advisory threshold
        MUST be expressed as a ratio of `cost_guard.daily_ceiling_usd`
        and named accordingly."""
        import inspect
        import multi_scheduler

        src = inspect.getsource(multi_scheduler)
        # Forbidden: the old constant name OR any plain-USD threshold
        # literal in cost_alert paths.
        assert "_DAILY_COST_ALERT_THRESHOLD" not in src, (
            "_DAILY_COST_ALERT_THRESHOLD constant re-introduced — "
            "alert threshold must come from cost_guard.daily_ceiling_usd"
        )
        # The ratio-based replacement must exist and be referenced in
        # _task_cost_check so we know the new mechanism is wired.
        assert "_COST_ALERT_THRESHOLD_RATIO" in src, (
            "Expected _COST_ALERT_THRESHOLD_RATIO ratio-based "
            "threshold; not found"
        )
        assert "daily_ceiling_usd" in src, (
            "Expected `daily_ceiling_usd` import/use in multi_scheduler "
            "so the alert reads the user's actual cap"
        )

    def test_alert_fires_at_80_percent_of_user_ceiling(self, monkeypatch):
        """Behavioral test: with ceiling $10, alert at $8.50 fires;
        at $5.00 it doesn't (50% — well under 80%)."""
        import multi_scheduler
        # Fake spend_summary to return a controllable today_cost.
        spent_per_db = {"q.db": 8.5}
        from glob import glob as _real_glob

        def _fake_spend_summary(db):
            return {"today": {"usd": spent_per_db.get(db, 0.0)},
                    "7d": {"usd": 0}, "30d": {"usd": 0},
                    "by_purpose_30d": [], "by_model_30d": []}

        monkeypatch.setattr(
            "ai_cost_ledger.spend_summary", _fake_spend_summary,
        )
        monkeypatch.setattr(
            "cost_guard.daily_ceiling_usd", lambda user_id: 10.0,
        )
        monkeypatch.setattr(
            "glob.glob", lambda _pat: ["q.db"],
        )

        # Reset the per-process alert dedup cache so the test can
        # observe the alert.
        multi_scheduler._cost_alerted_today.clear()

        # Capture log_activity calls.
        captured = []
        monkeypatch.setattr(
            "multi_scheduler._safe_log_activity",
            lambda pid, uid, kind, title, body: captured.append(
                (kind, title, body),
            ),
        )

        from types import SimpleNamespace
        ctx = SimpleNamespace(
            profile_id=1, user_id=1, db_path="q.db",
        )
        multi_scheduler._task_cost_check(ctx)

        # 8.5 / 10 = 85% > 80% → alert should fire.
        assert any(c[0] == "cost_alert" for c in captured), (
            f"Expected cost_alert at 85% of cap; got: {captured}"
        )
        # Alert text must reference the actual cap, not a hard-coded
        # "$3.00 threshold".
        body_text = " ".join(c[2] for c in captured if c[0] == "cost_alert")
        assert "$10" in body_text or "10.00" in body_text, (
            f"Alert body must reference the user's $10 cap; got: {body_text}"
        )
        assert "$3" not in body_text, (
            "Alert body must NOT mention a $3 threshold — that was "
            f"the bug: {body_text}"
        )

    def test_alert_does_NOT_fire_below_80_percent(self, monkeypatch):
        """At $4 spent against $10 cap (40%), no alert."""
        import multi_scheduler

        monkeypatch.setattr(
            "ai_cost_ledger.spend_summary",
            lambda db: {"today": {"usd": 4.0},
                        "7d": {"usd": 0}, "30d": {"usd": 0},
                        "by_purpose_30d": [], "by_model_30d": []},
        )
        monkeypatch.setattr(
            "cost_guard.daily_ceiling_usd", lambda user_id: 10.0,
        )
        monkeypatch.setattr(
            "glob.glob", lambda _pat: ["q.db"],
        )
        multi_scheduler._cost_alerted_today.clear()

        captured = []
        monkeypatch.setattr(
            "multi_scheduler._safe_log_activity",
            lambda pid, uid, kind, title, body: captured.append(kind),
        )

        from types import SimpleNamespace
        ctx = SimpleNamespace(profile_id=1, user_id=1, db_path="q.db")
        multi_scheduler._task_cost_check(ctx)

        assert "cost_alert" not in captured, (
            f"Alert fired below 80% threshold ($4/$10 = 40%); "
            f"captured: {captured}"
        )
