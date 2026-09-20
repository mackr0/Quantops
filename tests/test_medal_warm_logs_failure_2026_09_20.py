"""2026-09-20 — a failing medal warm is logged, never an unhandled
thread exception.

The profile-medal warm runs the dashboard-totals computation in a
background thread. The thread's target was that function itself, so
any failure escaped the thread unhandled: stderr only, never the log.
It surfaced as an unhandled-thread-exception warning in the suite (the
thread outlived its test and queried a torn-down database).
"""
from __future__ import annotations

import logging
import os
import re


def test_a_failing_warm_is_logged_and_does_not_raise(monkeypatch, caplog):
    import views

    def boom(uid):
        raise RuntimeError("no such table: trading_profiles")
    monkeypatch.setattr(views, "_dashboard_totals_payload", boom)
    with caplog.at_level(logging.WARNING, logger="views"):
        views._warm_medals(42)          # must not raise
    assert any("medal warm for user 42 failed" in r.getMessage()
               and "RuntimeError" in r.getMessage()
               for r in caplog.records)


def test_a_working_warm_computes_the_payload(monkeypatch):
    import views
    seen = []
    monkeypatch.setattr(views, "_dashboard_totals_payload", seen.append)
    views._warm_medals(7)
    assert seen == [7]


def test_the_thread_runs_the_logging_wrapper_not_the_bare_function():
    src = open(os.path.join(os.path.dirname(__file__), os.pardir,
                            "views.py")).read()
    targets = re.findall(r"threading\.Thread\(\s*target=(\w+)", src)
    assert "_warm_medals" in targets
    assert "_dashboard_totals_payload" not in targets, (
        "a bare thread target lets its exception escape unlogged")
