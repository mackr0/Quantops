"""Guardrail: walk_forward_analysis and out_of_sample_degradation
must call backtest_strategy with disjoint date ranges, NOT with
days=N (which would silently re-introduce overlapping windows).

History: 2026-04-27 methodology audit. Wave 2 / Fixes #3 and #4 of
METHODOLOGY_FIX_PLAN.md. The original wrappers passed
`days=fold_days` (walk-forward) and `days=in_sample_days` /
`days=oos_days` (OOS) to backtest_strategy. Because backtest_strategy
anchored on datetime.now(), every fold and the OOS window all read
the same recent data — there was no actual temporal separation.

These tests:

1. AST-walk both wrapper functions; fail if `days=` is passed to
   `backtest_strategy(...)` (only `start_date` / `end_date` allowed).
2. Behavioral: mock backtest_strategy and record (start_date,
   end_date) of each call. Walk-forward folds must be pairwise
   disjoint. OOS in-sample and out-of-sample must not overlap.
"""

from __future__ import annotations

import inspect
import re
from unittest.mock import patch

import rigorous_backtest


# ---------------------------------------------------------------------------
# Source-level guardrails — kill the regression at compile time
# ---------------------------------------------------------------------------

def test_walk_forward_does_not_call_backtest_with_days():
    """A walk-forward fold MUST call backtest_strategy with explicit
    dates. Falling back to days= silently overlaps fold windows."""
    src = inspect.getsource(rigorous_backtest.walk_forward_analysis)
    # Look for `backtest_strategy(` followed by anything containing
    # `days=` — that's the broken pattern.
    bt_calls = re.findall(
        r"backtest_strategy\s*\([^)]*\bdays\s*=", src, flags=re.DOTALL
    )
    assert not bt_calls, (
        "REGRESSION: walk_forward_analysis is passing `days=` to "
        "backtest_strategy again. That parameter anchors to "
        "datetime.now(), so every fold reads overlapping recent "
        "data. Use start_date / end_date instead. See "
        "METHODOLOGY_FIX_PLAN.md."
    )


def test_walk_forward_passes_start_and_end_date():
    """Positive guard: the wrapper must use the date-range path."""
    src = inspect.getsource(rigorous_backtest.walk_forward_analysis)
    assert "start_date=" in src and "end_date=" in src, (
        "walk_forward_analysis must call backtest_strategy with "
        "start_date / end_date arguments to read disjoint folds."
    )


def test_oos_does_not_call_backtest_with_days():
    src = inspect.getsource(rigorous_backtest.out_of_sample_degradation)
    bt_calls = re.findall(
        r"backtest_strategy\s*\([^)]*\bdays\s*=", src, flags=re.DOTALL
    )
    assert not bt_calls, (
        "REGRESSION: out_of_sample_degradation is passing `days=` "
        "again. That makes the IS and OOS windows both anchor to "
        "today, so OOS is INSIDE in-sample. Use start_date / end_date."
    )


def test_oos_passes_start_and_end_date():
    src = inspect.getsource(rigorous_backtest.out_of_sample_degradation)
    assert "start_date=" in src and "end_date=" in src, (
        "out_of_sample_degradation must call backtest_strategy with "
        "start_date / end_date so IS and OOS are disjoint calendar "
        "periods."
    )


# ---------------------------------------------------------------------------
# Behavioral: capture the date ranges of each call and verify disjointness
# ---------------------------------------------------------------------------

class _Recorder:
    """Stand-in for backtest_strategy that records the date ranges
    each call asked for, and returns a deterministic dummy result."""

    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append({
            "start_date": kwargs.get("start_date"),
            "end_date": kwargs.get("end_date"),
            "days": kwargs.get("days"),
        })
        return {
            "total_return_pct": 1.0,
            "sharpe_ratio": 0.5,
            "num_trades": 10,
        }


def test_walk_forward_folds_are_pairwise_disjoint():
    """Run walk_forward with 4 folds and 360 days; assert no two
    folds share a date in their (start, end) intervals."""
    recorder = _Recorder()
    with patch("backtester.backtest_strategy", recorder):
        rigorous_backtest.walk_forward_analysis(
            strategy_fn=lambda symbol, df: {"signal": "HOLD"},
            market_type="midcap",
            history_days=360,
            folds=4,
            params=None,
            initial_capital=10_000,
            sample_size=5,
            symbols=["AAPL"],
        )

    assert len(recorder.calls) == 4, (
        f"Expected 4 fold calls, got {len(recorder.calls)}"
    )

    # Every call must use start_date / end_date, not days=
    for call in recorder.calls:
        assert call["start_date"] is not None, (
            "Fold call missing start_date — wrapper still uses days="
        )
        assert call["end_date"] is not None
        assert call["days"] is None, (
            f"Fold call passed days={call['days']} — that's the broken "
            f"path. Should pass start_date / end_date only."
        )

    # Pairwise disjoint check (folds k and k+1 must not overlap)
    from datetime import date
    intervals = [
        (date.fromisoformat(c["start_date"]),
         date.fromisoformat(c["end_date"]))
        for c in recorder.calls
    ]
    for i in range(len(intervals)):
        for j in range(i + 1, len(intervals)):
            a_start, a_end = intervals[i]
            b_start, b_end = intervals[j]
            # Allow exact-touching boundaries (a_end == b_start) since
            # that's still disjoint in interval terms; flag any real
            # overlap.
            overlap = (a_start < b_end) and (b_start < a_end)
            assert not overlap, (
                f"Fold {i + 1} ({a_start}, {a_end}) overlaps fold "
                f"{j + 1} ({b_start}, {b_end}) — folds must be "
                f"pairwise disjoint for walk-forward to mean anything."
            )


def test_oos_in_sample_and_out_of_sample_do_not_overlap():
    """The whole point of OOS. Run the wrapper, check the IS window
    ends at or before the OOS window starts."""
    recorder = _Recorder()
    with patch("backtester.backtest_strategy", recorder):
        rigorous_backtest.out_of_sample_degradation(
            strategy_fn=lambda symbol, df: {"signal": "HOLD"},
            market_type="midcap",
            history_days=360,
            params=None,
            initial_capital=10_000,
            oos_fraction=0.25,
            sample_size=5,
            symbols=["AAPL"],
        )

    assert len(recorder.calls) == 2, (
        f"Expected 2 calls (IS + OOS), got {len(recorder.calls)}"
    )

    is_call, oos_call = recorder.calls
    from datetime import date
    is_end = date.fromisoformat(is_call["end_date"])
    oos_start = date.fromisoformat(oos_call["start_date"])

    assert is_end <= oos_start, (
        f"In-sample window ends {is_end} but OOS window starts "
        f"{oos_start}. OOS must start at or after IS ends — "
        f"otherwise OOS contains training data and the test is "
        f"meaningless."
    )
