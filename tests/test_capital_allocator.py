"""Tests for Layer 9 — auto capital allocation.

Critical guarantee tested: profiles that share an Alpaca account
have their capital_scale rebalanced WITHIN that group only, so the
underlying real account is never over-committed. A solo profile (1
per account) always gets scale=1.0.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest


# ─────────────────────────────────────────────────────────────────────
# Per-account grouping (the critical constraint)
# ─────────────────────────────────────────────────────────────────────

class TestPerAccountGrouping:
    def test_solo_profile_always_gets_scale_1(self):
        """A profile alone on its Alpaca account must stay at 1.0 —
        nothing to rebalance against."""
        from capital_allocator import _allocate
        # 3 profiles, 3 different alpaca accounts -> all solo
        profiles = [
            {"id": 1, "alpaca_account_id": 100, "score": 5.0,
             "capital_scale": 1.0},
            {"id": 2, "alpaca_account_id": 200, "score": -2.0,
             "capital_scale": 1.0},
            {"id": 3, "alpaca_account_id": 300, "score": 0.0,
             "capital_scale": 1.0},
        ]
        result = _allocate(profiles)
        assert result == {1: 1.0, 2: 1.0, 3: 1.0}

    def test_shared_account_sums_to_n(self):
        """Profiles sharing an account must have their scales sum to N
        (the count in the group). Otherwise the underlying $X account
        is either over- or under-committed."""
        from capital_allocator import _allocate
        # 3 profiles share account 100
        profiles = [
            {"id": 1, "alpaca_account_id": 100, "score": 5.0,
             "capital_scale": 1.0},
            {"id": 2, "alpaca_account_id": 100, "score": 1.0,
             "capital_scale": 1.0},
            {"id": 3, "alpaca_account_id": 100, "score": -1.0,
             "capital_scale": 1.0},
        ]
        result = _allocate(profiles)
        # Sum should equal 3 (group size) within float tolerance
        total = sum(result.values())
        assert abs(total - 3.0) < 0.01

    def test_higher_scoring_gets_more_capital(self):
        from capital_allocator import _allocate
        profiles = [
            {"id": 1, "alpaca_account_id": 100, "score": 5.0,
             "capital_scale": 1.0},   # winner
            {"id": 2, "alpaca_account_id": 100, "score": -3.0,
             "capital_scale": 1.0},   # loser
        ]
        result = _allocate(profiles)
        assert result[1] > result[2]
        # Sum still conserved
        assert abs(sum(result.values()) - 2.0) < 0.01

    def test_mixed_groups_respect_independence(self):
        """One account has 2 profiles, another has 1. The 2-profile
        group rebalances within itself; the solo stays at 1.0."""
        from capital_allocator import _allocate
        profiles = [
            {"id": 1, "alpaca_account_id": 100, "score": 5.0,
             "capital_scale": 1.0},
            {"id": 2, "alpaca_account_id": 100, "score": -1.0,
             "capital_scale": 1.0},
            {"id": 3, "alpaca_account_id": 200, "score": 10.0,
             "capital_scale": 1.0},
        ]
        result = _allocate(profiles)
        # Group {1,2} sums to 2
        assert abs(result[1] + result[2] - 2.0) < 0.01
        # Profile 3 (solo) at 1.0 regardless of its score
        assert result[3] == 1.0


# ─────────────────────────────────────────────────────────────────────
# Bounds clamping
# ─────────────────────────────────────────────────────────────────────

class TestBoundsClamping:
    def test_per_rebalance_change_capped_at_50pct(self):
        """A profile's scale shouldn't be able to move more than ±50%
        in a single rebalance, even if the score swing is huge."""
        from capital_allocator import _allocate
        # Profile 1 has dominant score; would naively push to ~2.0
        # but starting at 1.0 it can only go to 1.5 in one step.
        profiles = [
            {"id": 1, "alpaca_account_id": 100, "score": 100.0,
             "capital_scale": 1.0},
            {"id": 2, "alpaca_account_id": 100, "score": -50.0,
             "capital_scale": 1.0},
        ]
        result = _allocate(profiles)
        # After clamping + group-conservation re-normalize, the
        # winner shouldn't exceed 1.5 by much (some headroom from
        # re-normalization, but should be well under 2x).
        assert result[1] <= 1.6
        assert result[2] >= 0.4

    def test_absolute_bounds_respected(self):
        """capital_scale ∈ [0.25, 2.0] absolute, even after multiple
        rebalances would push past."""
        from capital_allocator import _allocate
        profiles = [
            {"id": 1, "alpaca_account_id": 100, "score": 1000.0,
             # Already at ceiling
             "capital_scale": 2.0},
            {"id": 2, "alpaca_account_id": 100, "score": -1000.0,
             # Already at floor
             "capital_scale": 0.25},
        ]
        result = _allocate(profiles)
        assert result[1] <= 2.0
        assert result[2] >= 0.25


# ─────────────────────────────────────────────────────────────────────
# Opt-in gate
# ─────────────────────────────────────────────────────────────────────

class TestOptInGate:
    def test_rebalance_no_op_when_disabled(self, tmp_path):
        from capital_allocator import rebalance
        mock_conn = type("MockConn", (), {})()
        mock_conn.execute = lambda *a, **k: type("R", (), {
            "fetchone": lambda self=None: (0,)})()
        mock_conn.close = lambda: None
        with patch("models._get_conn", return_value=mock_conn):
            assert rebalance(1) == []
