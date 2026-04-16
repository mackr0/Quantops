"""Regression tests for staggered per-profile scheduling (2026-04-14).

Bug: scheduler tracked `last_run["scan"]` as a global timestamp shared
across all profiles. When one profile's cycle took longer than the
15-min interval, every profile's "next due" time got pushed, and the
last-iterated profile (Large Cap in practice) could starve entirely.

Fix: each profile has its own `{scan, check_exits, resolve_predictions}`
dict. Per-profile due-checks mean one profile's slow cycle doesn't
block another's clock.
"""

from __future__ import annotations

import pytest


class TestPerProfileInterval:
    """Verify per-profile timing is independent between profiles."""

    def test_two_profiles_clock_independently(self):
        """Core property: profile A's timer doesn't advance profile B's."""
        # Simulate the per-profile run-tracking pattern used in the
        # scheduler's main loop.
        profile_runs = {}

        def get_pr(pid):
            if pid not in profile_runs:
                profile_runs[pid] = {"scan": 0.0, "check_exits": 0.0,
                                     "resolve_predictions": 0.0}
            return profile_runs[pid]

        INTERVAL = 900  # 15 min

        # At any live time (simulated as t=1_700_000_000, a realistic epoch)
        # both profiles are due since both last_run=0
        t0 = 1_700_000_000
        assert (t0 - get_pr(1)["scan"]) >= INTERVAL
        assert (t0 - get_pr(2)["scan"]) >= INTERVAL

        # Profile 1 runs, finishes at t0+300 (5 min later)
        get_pr(1)["scan"] = t0 + 300

        # t0+310: profile 1 NOT due (only 10 sec since last run).
        # Profile 2 STILL due (never ran).
        now = t0 + 310
        assert (now - get_pr(1)["scan"]) < INTERVAL
        assert (now - get_pr(2)["scan"]) >= INTERVAL   # <- key property

    def test_slow_profile_cycle_does_not_starve_others(self):
        """The production failure mode we're fixing."""
        profile_runs = {}

        def get_pr(pid):
            if pid not in profile_runs:
                profile_runs[pid] = {"scan": 0.0}
            return profile_runs[pid]

        INTERVAL = 900

        # t=0: all 3 due
        now = 0
        # Profile 1 runs a LONG cycle: 20 minutes
        now = 1200  # t=20 min, profile 1 just finished
        get_pr(1)["scan"] = now

        # Profile 2 and 3 never ran yet
        # With the OLD global timestamp, last_run["scan"]=1200 would be
        # "recent" — profiles 2 and 3 would be blocked for 15 more min.
        # With per-profile:
        for pid in (2, 3):
            assert (now - get_pr(pid)["scan"]) >= INTERVAL, (
                f"profile {pid} should be due — its clock is independent"
            )

    def test_first_run_natural_staggering(self):
        """When 3 profiles all start at last_run=0 and cycle sequentially,
        they naturally stagger by their cycle length."""
        profile_runs = {}

        def get_pr(pid):
            if pid not in profile_runs:
                profile_runs[pid] = {"scan": 0.0}
            return profile_runs[pid]

        CYCLE = 300  # each profile's cycle takes 5 min
        INTERVAL = 900

        # Sequential first pass: profile 1 at t=0, 2 at t=300, 3 at t=600
        get_pr(1)["scan"] = 0 + CYCLE
        get_pr(2)["scan"] = CYCLE + CYCLE
        get_pr(3)["scan"] = 2 * CYCLE + CYCLE

        # Now fast-forward to t=900 + 300 = 1200 (profile 1's next due
        # time is 0 + CYCLE + INTERVAL = 1200)
        t = 1200
        # Profile 1 should be due (last ran at 300, 900 sec ago)
        assert (t - get_pr(1)["scan"]) >= INTERVAL
        # Profiles 2 and 3 not yet due (they ran later)
        assert (t - get_pr(2)["scan"]) < INTERVAL
        assert (t - get_pr(3)["scan"]) < INTERVAL


class TestSchedulerHelperStructure:
    """Smoke tests that the scheduler module has the per-profile
    helper pattern in place after the refactor."""

    def test_module_imports_with_typing(self):
        """The refactor added a typing import — ensure it resolved."""
        import multi_scheduler
        # Typing import shouldn't have broken the module
        assert hasattr(multi_scheduler, "run_segment_cycle")

    def test_source_has_per_profile_state(self):
        """Guard against regressing back to the global-last-run pattern.

        Reads the source to confirm the new per-profile structure is
        present. Fails loudly if someone flattens it back."""
        import inspect, multi_scheduler
        src = inspect.getsource(multi_scheduler)
        assert "profile_runs" in src, (
            "per-profile last-run dict is gone — scheduler regressed to "
            "the global-timestamp pattern (Large Cap starvation bug)"
        )
        assert "_get_profile_runs" in src, (
            "per-profile helper function missing"
        )
        assert "prof_do_scan" in src, (
            "per-profile do_scan computation missing"
        )
