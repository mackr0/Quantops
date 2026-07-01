"""Scan cadence matches the configured interval (2026-07-01).

At a 10-min scan interval the fleet was cycling every ~13-17 min. Two causes,
both fixed here:
  1. The per-profile run clock was stamped at cycle FINISH, so cadence =
     interval + cycle duration (~1.7 min) + overhead. Now stamped at START,
     so the interval IS the cadence (with a rollback on failure so a crashed
     cycle still retries next iteration rather than skipping an interval).
  2. The worker pool was capped at 3, so profiles that came due together
     queued behind a wave. Raised to _CYCLE_MAX_WORKERS (6) — scans are
     I/O-bound on LLM calls, so more threads help even on the 2-CPU box.
"""
from __future__ import annotations

import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import multi_scheduler


def test_worker_cap_raised_and_used():
    assert multi_scheduler._CYCLE_MAX_WORKERS >= 6
    src = inspect.getsource(multi_scheduler.main_loop)
    assert "min(len(items), _CYCLE_MAX_WORKERS)" in src, (
        "the cycle worker pool must use _CYCLE_MAX_WORKERS, not a hardcoded 3")


def test_run_clock_stamped_at_start_not_finish():
    src = inspect.getsource(multi_scheduler.main_loop)
    # the scan clock is stamped BEFORE run_segment_cycle runs
    i_stamp = src.index('pr["scan"] = start_t')
    i_run = src.index("run_segment_cycle(", i_stamp)
    assert i_stamp < i_run, (
        "last_scan must be stamped at cycle START (before run_segment_cycle) "
        "so the configured interval is the actual cadence")
    # no leftover finish-stamp of the scan clock
    assert 'pr["scan"] = finish_t' not in src


def test_failed_cycle_rolls_the_clock_back():
    """A crashed cycle must restore the prior stamps so it retries next
    iteration instead of waiting a full interval."""
    src = inspect.getsource(multi_scheduler.main_loop)
    assert "_prior" in src
    # the restore happens in an except path and re-raises
    assert "pr[_k] = _v" in src
    body = src.split("def _run_one_profile", 1)[1][:2500]
    assert "except Exception:" in body and "raise" in body
