"""Pin docs/18 item #2: nightly Phase 5c backfill task.

The boot-time call in `cycle_segment` is gated by a migration
marker — it runs ONCE per profile DB and no-ops thereafter. Any
historical option row that gets created or re-resolved AFTER that
first run with bad math would otherwise stay broken forever.

`_task_phase5c_backfill_nightly` fixes that by calling
`backfill_historical_option_predictions(force=True)` each day. The
row-level WHERE clause (`option_order_id IS NULL AND occ_symbol
IS NULL`) keeps the call cheap when there's nothing to do.

These tests pin:
  1. force=True is actually passed (otherwise the migration marker
     would skip the work)
  2. Clean DB → "nothing to do" log, no activity row
  3. Dirty DB (something linked) → activity row written
  4. Backfill failures are contained — no exception escapes the task
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


class _Ctx:
    def __init__(self, db_path="x.db", profile_id=12, user_id=1,
                 display_name="TEST", segment="stocks"):
        self.db_path = db_path
        self.profile_id = profile_id
        self.user_id = user_id
        self.display_name = display_name
        self.segment = segment


# ---------------------------------------------------------------------------
# (1) force=True is actually passed
# ---------------------------------------------------------------------------

def test_task_calls_backfill_with_force_true(monkeypatch):
    """If the task forgot force=True the migration marker would
    short-circuit the call on every cycle after the first run —
    breaking the whole point of the nightly task."""
    seen = {}

    def _fake_backfill(db_path, force=False):
        seen["force"] = force
        seen["db_path"] = db_path
        return {"scanned": 0, "linked_multileg": 0,
                "linked_single_leg": 0, "no_match": 0,
                "skipped_already_done": 0}

    monkeypatch.setattr(
        "pipelines.outcomes.backfill.backfill_historical_option_predictions",
        _fake_backfill,
    )
    from multi_scheduler import _task_phase5c_backfill_nightly
    _task_phase5c_backfill_nightly(_Ctx(db_path="/tmp/x.db"))
    assert seen["force"] is True
    assert seen["db_path"] == "/tmp/x.db"


# ---------------------------------------------------------------------------
# (2) Clean DB → no activity row
# ---------------------------------------------------------------------------

def test_clean_db_no_activity_row(monkeypatch):
    """When scanned == 0, the task logs but does NOT write an
    activity row — operator inbox stays quiet on the steady state."""
    monkeypatch.setattr(
        "pipelines.outcomes.backfill.backfill_historical_option_predictions",
        lambda db_path, force=False: {
            "scanned": 0, "linked_multileg": 0,
            "linked_single_leg": 0, "no_match": 0,
            "skipped_already_done": 0,
        },
    )
    activity_calls = []
    monkeypatch.setattr(
        "multi_scheduler._safe_log_activity",
        lambda *a, **kw: activity_calls.append((a, kw)),
    )
    from multi_scheduler import _task_phase5c_backfill_nightly
    _task_phase5c_backfill_nightly(_Ctx())
    assert activity_calls == []


# ---------------------------------------------------------------------------
# (3) Dirty DB (rows linked) → activity row written
# ---------------------------------------------------------------------------

def test_linked_rows_produce_activity_row(monkeypatch):
    monkeypatch.setattr(
        "pipelines.outcomes.backfill.backfill_historical_option_predictions",
        lambda db_path, force=False: {
            "scanned": 5, "linked_multileg": 3,
            "linked_single_leg": 1, "no_match": 1,
            "skipped_already_done": 0,
        },
    )
    activity_calls = []
    monkeypatch.setattr(
        "multi_scheduler._safe_log_activity",
        lambda *a, **kw: activity_calls.append((a, kw)),
    )
    from multi_scheduler import _task_phase5c_backfill_nightly
    _task_phase5c_backfill_nightly(_Ctx())
    assert len(activity_calls) == 1
    args, _ = activity_calls[0]
    # Signature: (profile_id, user_id, type, title, detail)
    assert args[2] == "phase5c_backfill"
    assert "4" in args[3]      # 4 linked total (3 multileg + 1 single-leg)
    assert "multileg=3" in args[4]
    assert "single-leg=1" in args[4]


def test_only_scanned_no_links_no_activity(monkeypatch):
    """If we scanned 5 rows but linked 0 (all `no_match`), no
    activity row — the operator only hears about NEW LINKS, not
    "we looked again and found nothing"."""
    monkeypatch.setattr(
        "pipelines.outcomes.backfill.backfill_historical_option_predictions",
        lambda db_path, force=False: {
            "scanned": 5, "linked_multileg": 0,
            "linked_single_leg": 0, "no_match": 5,
            "skipped_already_done": 0,
        },
    )
    activity_calls = []
    monkeypatch.setattr(
        "multi_scheduler._safe_log_activity",
        lambda *a, **kw: activity_calls.append((a, kw)),
    )
    from multi_scheduler import _task_phase5c_backfill_nightly
    _task_phase5c_backfill_nightly(_Ctx())
    assert activity_calls == []


# ---------------------------------------------------------------------------
# (4) Backfill crash is contained
# ---------------------------------------------------------------------------

def test_backfill_exception_does_not_propagate(monkeypatch):
    """A crash inside backfill (e.g. DB locked, schema drift) must
    NOT raise out of the task — the rest of the scheduler cycle
    has to continue."""
    def _boom(db_path, force=False):
        raise RuntimeError("synthetic backfill crash")

    monkeypatch.setattr(
        "pipelines.outcomes.backfill.backfill_historical_option_predictions",
        _boom,
    )
    from multi_scheduler import _task_phase5c_backfill_nightly
    # MUST NOT raise
    _task_phase5c_backfill_nightly(_Ctx())
