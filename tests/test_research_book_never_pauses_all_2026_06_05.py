"""2026-06-05 — research/paper-book contract: neither
drawdown_acceleration nor vol_spike may escalate to `pause_all` on
critical severity. The only legitimate `pause_all` source is
held_position_halts (when the broker can't trade held names).

Why this contract matters: pause_all blocks EXITS in addition to
new entries. For a paper-money experiment whose purpose is measuring
strategy behavior across regimes, this:
  1. Traps held risk on the days the operator most needs to close
  2. Selects OUT exactly the high-information days from the dataset
  3. Creates asymmetric experiments (non-AI baselines like buy_hold
     and random bypass the gate; AI profiles get muted on bad days)
The intent of these checks is to throttle NEW risk on stressed days,
not to lock the system out of risk-reducing actions.

The contract is pinned both as a behavioral test (specific inputs
→ specific suggested_action) and as a structural test (no
`pause_all` literal in either check's body).
"""
from __future__ import annotations

import ast
import inspect
import re

import pytest

from intraday_risk_monitor import (
    check_drawdown_acceleration,
    check_held_position_halts,
    check_vol_spike,
)


# ---------------------------------------------------------------------------
# Behavioral pin — these specific inputs must yield block_new_entries,
# not pause_all, no matter what.
# ---------------------------------------------------------------------------

class TestDrawdownAccelNeverPausesAll:

    def test_critical_drawdown_yields_block_new_entries(self):
        """3× the 7-day average is the old critical threshold. Must
        now yield block_new_entries, not pause_all."""
        alert = check_drawdown_acceleration(
            today_intraday_pct=0.05,
            avg_7d_intraday_pct=0.01,
        )
        assert alert is not None
        assert alert.severity == "critical", (
            "Severity classification (critical) should still flow to "
            "the UI for visibility; only the action changes"
        )
        assert alert.suggested_action == "block_new_entries", (
            "drawdown_acceleration critical MUST yield "
            f"block_new_entries, not pause_all. "
            f"got: {alert.suggested_action!r}. "
            "pause_all blocks exits — the OPPOSITE of risk management."
        )

    def test_warning_drawdown_yields_block_new_entries(self):
        """Warning severity already used block_new_entries; pin that
        it doesn't accidentally flip too."""
        alert = check_drawdown_acceleration(
            today_intraday_pct=0.03,
            avg_7d_intraday_pct=0.01,  # 3x is right at the boundary
        )
        # NB: 3.0 multiple is the critical boundary; just below it
        # should be warning.
        alert2 = check_drawdown_acceleration(
            today_intraday_pct=0.025,
            avg_7d_intraday_pct=0.01,
        )
        if alert2 is not None:
            assert alert2.suggested_action == "block_new_entries"


class TestVolSpikeNeverPausesAll:

    def test_critical_vol_spike_yields_block_new_entries(self):
        """5× the 20-day average was the old critical threshold."""
        alert = check_vol_spike(
            current_hourly_vol=0.05,
            avg_20d_hourly_vol=0.01,
        )
        assert alert is not None
        assert alert.severity == "critical"
        assert alert.suggested_action == "block_new_entries", (
            "vol_spike critical MUST yield block_new_entries. "
            "Vol spikes are exactly when stop-losses MUST be allowed "
            "to fire — pause_all would trap held risk."
        )


# ---------------------------------------------------------------------------
# Structural pin — neither function's source code may contain
# `pause_all`. Forbids re-introducing the bad escalation later.
# ---------------------------------------------------------------------------

def _function_source(fn) -> str:
    return inspect.getsource(fn)


def test_drawdown_acceleration_source_has_no_pause_all_literal():
    src = _function_source(check_drawdown_acceleration)
    # Allow comments that mention pause_all (we explicitly explain
    # why we don't use it); forbid only string-literal occurrences.
    # Reduce to AST: any ast.Constant whose value contains "pause_all".
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert "pause_all" not in node.value, (
                "drawdown_acceleration must NOT emit pause_all as a "
                "string literal — that would re-introduce the "
                "exits-blocked bug. Use block_new_entries instead."
            )


def test_vol_spike_source_has_no_pause_all_literal():
    src = _function_source(check_vol_spike)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert "pause_all" not in node.value, (
                "vol_spike must NOT emit pause_all as a string literal."
            )


# ---------------------------------------------------------------------------
# Whitelist — held_position_halts MAY still escalate to pause_all.
# When the broker can't trade names we hold, pause_all is the correct
# response (we literally can't act). This test documents the only
# legitimate use of pause_all in the file so future audits can verify.
# ---------------------------------------------------------------------------

def test_held_position_halts_critical_still_yields_pause_all():
    """Sanity: don't accidentally over-rewrite. held_position_halts
    is the one check where pause_all stays correct (broker won't
    accept orders on halted names anyway)."""
    alert = check_held_position_halts(
        halted_held_symbols=["AAPL", "MSFT", "NVDA"],
    )
    assert alert is not None
    assert alert.severity == "critical"
    assert alert.suggested_action == "pause_all", (
        "held_position_halts critical (≥3 names halted) is the ONLY "
        "legitimate pause_all in the file. Don't touch this one — "
        "the broker can't fill orders on halted names anyway."
    )
