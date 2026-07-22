"""2026-07-22 — ORDER-NOT-FOUND voiding in the fill-healer.

p212's GOOG 375/380 close pair sat status='pending_fill' for a WEEK
with $4,608 of booked pnl: the submits died in the 429 storm, the
broker never learned the order_ids, and get_order raised "order not
found" — which `_task_update_fills` treated as a transient error and
skipped forever. check_decomposition counts pending_fill pnl as
realized, so the phantom $4,608 held the integrity kill switch
latched with a CONSTANT gap.

Pins (source-level, on the healer's exception path):
  1. a 404/"not found" on a >24h-old row voids it (status='canceled')
  2. the void is age-gated (a fresh order's propagation delay can
     never void a real trade)
  3. non-404 errors keep the old skip behavior
  4. the repair script exists, dry-runs by default, and only voids on
     broker-confirmed 404 / terminal-unfilled verdicts
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _healer_exc_block():
    src = open(os.path.join(REPO, "multi_scheduler.py")).read()
    idx = src.index("ORDER-NOT-FOUND voiding")
    return src[idx:idx + 2400]


class TestHealerVoidsUnknownOrders:

    def test_404_voids_row(self):
        block = _healer_exc_block()
        assert "'not found' in _es" in block or '"not found" in _es' in block
        assert "status = 'canceled'" in block, (
            "a >24h order-not-found row must be voided, not skipped "
            "forever — the $4,608 phantom-pnl latch")

    def test_void_is_age_gated(self):
        block = _healer_exc_block()
        assert "timedelta(hours=24)" in block, (
            "the void must be age-gated at 24h so broker-side "
            "propagation delay on a fresh order can never void a real "
            "trade")
        assert "_row_ts < _cutoff" in block or "_row_ts and _row_ts < _cutoff" in block

    def test_non_404_keeps_skip_behavior(self):
        block = _healer_exc_block()
        assert "continue" in block
        # the debug-and-continue fallback must survive for other errors
        assert "get_order(%s) failed" in block


class TestRepairScript:

    def _src(self):
        return open(os.path.join(
            REPO, "scripts",
            "repair_phantom_close_pnl_2026_07_22.py")).read()

    def test_dry_run_by_default(self):
        src = self._src()
        assert '"--apply"' in src and "store_true" in src
        assert "DRY RUN" in src

    def test_voids_only_on_broker_confirmed_verdicts(self):
        src = self._src()
        assert "not found" in src
        assert "terminal-unfilled" in src
        assert "NOT voiding" in src, (
            "a live-or-filled order must be reported and skipped — "
            "the script never guesses")

    def test_targets_pending_fill_older_than_24h(self):
        src = self._src()
        assert "status = 'pending_fill'" in src
        assert "timedelta(hours=24)" in src
