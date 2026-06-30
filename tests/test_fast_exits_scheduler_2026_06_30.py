"""Exits run in a fast pass BEFORE the slow entry scans (2026-06-30).

Protective stop/TP checks are free (deterministic, zero LLM) and time-critical,
but they used to share the scheduler's 3-worker pool with the LLM entry scans
and lagged ~13 min on the 13-profile fleet. The loop now runs exit/maintenance
cycles (anything not due for an entry scan) in a dedicated pass ahead of the
scan pass — so exits fire on their ~5-min timer, while the integrity gate still
guards the (slower, later) entry scans.
"""
from __future__ import annotations

import inspect
import os

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def test_split_buckets_exits_and_maintenance_separately_from_scans():
    from multi_scheduler import _split_due_for_fast_exits
    items = [
        {"prof": {"name": "scan+exit"}, "do_scan": True, "do_exits": True},
        {"prof": {"name": "exit-only"}, "do_scan": False, "do_exits": True},
        {"prof": {"name": "predict-only"}, "do_scan": False, "do_exits": False,
         "do_predictions": True},
        {"prof": {"name": "scan-only"}, "do_scan": True, "do_exits": False},
    ]
    non_scan, scan_due = _split_due_for_fast_exits(items)
    non_scan_names = {it["prof"]["name"] for it in non_scan}
    scan_names = {it["prof"]["name"] for it in scan_due}
    # exit-only + predict-only run in the fast (pass-1) bucket
    assert non_scan_names == {"exit-only", "predict-only"}
    # anything due for an entry scan is in pass 2
    assert scan_names == {"scan+exit", "scan-only"}
    # every item lands in exactly one bucket (no drops, no dupes)
    assert len(non_scan) + len(scan_due) == len(items)


def test_empty_input_is_safe():
    from multi_scheduler import _split_due_for_fast_exits
    assert _split_due_for_fast_exits([]) == ([], [])


def test_loop_runs_exits_before_integrity_gate_before_scans():
    """Ordering contract: the fast exit/maintenance pass dispatches BEFORE the
    integrity gate, which runs BEFORE the entry-scan pass."""
    from multi_scheduler import main_loop
    src = inspect.getsource(main_loop)
    i_exits = src.index('_run_pool(non_scan')
    i_gate = src.index('_run_integrity_gate()', i_exits)
    i_scan = src.index('_run_pool(scan_due', i_gate)
    assert i_exits < i_gate < i_scan, (
        "exit/maintenance pass must run before the integrity gate, which must "
        "run before the entry-scan pass")
