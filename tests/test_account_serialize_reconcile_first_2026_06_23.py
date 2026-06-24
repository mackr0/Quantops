"""Slice 3 — reconcile-first (2026-06-23).

Each profile is an independent entity: its position is the signed sum of its
OWN order_id fills, and it only ever acts on its own book. Before any
exit/protective/option logic runs, the profile's journal is freshened to its
own broker truth (reconcile-first) so decisions never act on a stale book.

(An earlier draft also serialized same-account profiles behind a per-account
lock; that was removed — profiles are independent and the oversell door already
limits each to its own fresh-owned shares, so no cross-profile coordination is
needed. See CHANGELOG 2026-06-23.)
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def test_task_freshen_to_broker_reconciles_and_stamps():
    """The reconcile-first task brings the profile to its own broker truth and
    stamps every symbol fresh (via reconcile_and_stamp) before exits run."""
    import multi_scheduler as M
    ctx = SimpleNamespace(db_path="x", alpaca_account_id="45")
    with patch("reconcile_journal_to_broker.reconcile_and_stamp") as m:
        M._task_freshen_to_broker(ctx)
        m.assert_called_once_with(ctx)


def test_profiles_run_without_cross_profile_coordination():
    """Regression guard for the independence principle: the scheduler must not
    reintroduce any per-account serialization lock — profiles are independent
    and coordinate through nothing but the broker conduit."""
    import multi_scheduler as M
    assert not hasattr(M, "_account_lock"), (
        "per-account serialization was removed: profiles are independent "
        "legal entities; the oversell door already limits each to its own "
        "fresh-owned shares, so no cross-profile lock may exist")
