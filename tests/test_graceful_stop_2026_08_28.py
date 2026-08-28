"""2026-08-28 — graceful stop converges between TASKS, not cycles.

The 600s systemd stop-grace (added 08-27) was necessary but not
sufficient: the scheduler honored SIGTERM only between whole cycles,
a full cycle can exceed 10 minutes, and the 14:14 deploy SIGKILLed
mid-cycle anyway. run_task — the wrapper every task passes through —
now skips not-yet-started tasks once shutdown is requested, bounding
stop time by the single longest RUNNING task.
"""
from __future__ import annotations

import pytest

import multi_scheduler as ms


@pytest.fixture(autouse=True)
def _restore_flag():
    prev = ms._shutdown
    yield
    ms._shutdown = prev


def test_run_task_skips_after_shutdown_signal(caplog):
    ms._shutdown = True
    calls = []
    import logging
    with caplog.at_level(logging.INFO):
        ms.run_task("Test Task", lambda: calls.append(1))
    assert calls == [], "a task must never START once shutdown is requested"
    assert any("TASK SKIP" in r.message and "shutting down" in r.message
               for r in caplog.records), (
        "the skip must be loud — silence would look like a stall")


def test_run_task_runs_normally_without_shutdown():
    ms._shutdown = False
    calls = []
    ms.run_task("Test Task", lambda: calls.append(1))
    assert calls == [1]


def test_signal_handler_sets_the_flag():
    ms._shutdown = False
    ms._handle_signal(15, None)
    assert ms._shutdown is True


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
