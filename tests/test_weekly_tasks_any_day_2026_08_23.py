"""Weekly scheduler tasks run by marker age, on any day (docs/25 4.1).

The post-mortem (learned patterns), stat-arb universe scan, capital
rebalance and auto-strategy generation were gated on `weekday() == 6`.
The fleet spends every night and weekend parked in the sleep loop, so
a Sunday-only task never ran unless the process restarted on a Sunday:
the post-mortem marker on prod read 2026-07-26 on 2026-08-23, and
`learned_patterns` had zero rows. Weekly now means "first task cycle
at least seven days after the last run".
"""
from __future__ import annotations

import inspect
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import multi_scheduler as ms  # noqa: E402


class TestWeeklyDue:
    def test_missing_marker_is_due(self, tmp_path):
        assert ms._weekly_task_due(str(tmp_path / "none.marker"), "2026-08-24")

    def test_recent_marker_is_not_due(self, tmp_path):
        m = tmp_path / "m.marker"
        m.write_text("2026-08-21")
        assert not ms._weekly_task_due(str(m), "2026-08-24")

    def test_seven_day_old_marker_is_due_on_a_weekday(self, tmp_path):
        m = tmp_path / "m.marker"
        m.write_text("2026-08-17")
        assert ms._weekly_task_due(str(m), "2026-08-24")   # a Monday

    def test_garbage_marker_is_due(self, tmp_path):
        m = tmp_path / "m.marker"
        m.write_text("not-a-date")
        assert ms._weekly_task_due(str(m), "2026-08-24")


class TestNoSundayGates:
    def test_no_task_is_gated_on_sunday(self):
        src = inspect.getsource(ms)
        assert "weekday() != 6" not in src, (
            "a Sunday-only gate never fires: the fleet sleeps weekends")

    def test_weekly_tasks_use_the_helper(self):
        for fn in (ms._task_post_mortem, ms._task_capital_rebalance,
                   ms._task_stat_arb_universe_scan,
                   ms._task_auto_strategy_generation):
            assert "_weekly_task_due(" in inspect.getsource(fn), fn.__name__
