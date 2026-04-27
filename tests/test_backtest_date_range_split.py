"""Guardrail: backtest_strategy must accept explicit date ranges
and produce disjoint slices for disjoint inputs.

History: 2026-04-27 methodology audit. Wrappers around
backtest_strategy were calling it with `days=N` (always anchored to
datetime.now()), so walk_forward_analysis and out_of_sample_degradation
read overlapping recent data instead of disjoint historical periods.

Wave 1 of the methodology fix plan (METHODOLOGY_FIX_PLAN.md) added a
new `start_date` / `end_date` parameter pair plus the
`_fetch_yf_history_range` helper that slices the cached full-history
DataFrame by date. These tests prove:

1. The new date-range parameters exist on the public API.
2. The helper returns bars in the requested range plus warmup.
3. Two date-range backtests with disjoint inputs read disjoint
   simulation data — the property that walk-forward and OOS need.

When this test fails, the fix has been undone.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta
from unittest.mock import patch

import pandas as pd
import pytest

import backtester


def _synthetic_full_df(start="2024-01-01", days=600):
    """Build a 600-day synthetic OHLC dataframe in the same shape as
    the cached data that `_fetch_yf_history_range` slices."""
    idx = pd.date_range(start=start, periods=days, freq="B")
    df = pd.DataFrame(
        {
            "open":   [100.0 + i * 0.1 for i in range(days)],
            "high":   [101.0 + i * 0.1 for i in range(days)],
            "low":    [99.0 + i * 0.1 for i in range(days)],
            "close":  [100.5 + i * 0.1 for i in range(days)],
            "volume": [1_000_000] * days,
        },
        index=idx,
    )
    return df


# ---------------------------------------------------------------------------
# Source-level guardrails
# ---------------------------------------------------------------------------

def test_backtest_strategy_accepts_start_and_end_date():
    """The public API must accept start_date and end_date kwargs.
    Walk-forward and OOS depend on this."""
    sig = inspect.signature(backtester.backtest_strategy)
    assert "start_date" in sig.parameters, (
        "REGRESSION: backtest_strategy lost its start_date parameter. "
        "Date-range callers (walk_forward, OOS) require explicit dates "
        "to read disjoint historical windows. See METHODOLOGY_FIX_PLAN.md."
    )
    assert "end_date" in sig.parameters, (
        "REGRESSION: backtest_strategy lost its end_date parameter."
    )


def test_fetch_yf_history_range_helper_exists():
    assert hasattr(backtester, "_fetch_yf_history_range"), (
        "REGRESSION: backtester._fetch_yf_history_range removed. This "
        "helper is the date-range counterpart of _fetch_yf_history; "
        "without it, date-range backtests fall back to the today-"
        "relative slicer and reintroduce the look-ahead bug."
    )


# ---------------------------------------------------------------------------
# Behavioral: helper returns the requested range
# ---------------------------------------------------------------------------

def test_fetch_range_returns_bars_inside_requested_window():
    """Given a known full_df in cache, slicing to [Aug, Sep] returns
    only Aug/Sep bars (plus warmup before Aug)."""
    full = _synthetic_full_df(start="2024-01-01", days=400)
    backtester._symbol_cache["TEST"] = {
        "data": full, "ts": __import__("time").time(),
    }
    try:
        sliced = backtester._fetch_yf_history_range(
            "TEST",
            start_date="2024-08-01",
            end_date="2024-09-30",
            warmup_days=30,
        )
    finally:
        backtester._symbol_cache.pop("TEST", None)

    assert sliced is not None
    # Includes warmup before start_date and ends at end_date inclusive
    assert sliced.index.min() <= pd.Timestamp("2024-08-01")
    assert sliced.index.max() <= pd.Timestamp("2024-09-30")
    # Warmup bound: warmup_days back from 2024-08-01 is 2024-07-02
    assert sliced.index.min() >= pd.Timestamp("2024-07-01")


def test_fetch_range_returns_none_when_window_outside_cache():
    full = _synthetic_full_df(start="2024-01-01", days=200)
    backtester._symbol_cache["TEST"] = {
        "data": full, "ts": __import__("time").time(),
    }
    try:
        sliced = backtester._fetch_yf_history_range(
            "TEST",
            start_date="2030-01-01",  # way after cached data
            end_date="2030-06-30",
            warmup_days=30,
        )
    finally:
        backtester._symbol_cache.pop("TEST", None)

    assert sliced is None or sliced.empty


# ---------------------------------------------------------------------------
# Behavioral: disjoint date ranges read disjoint data
# ---------------------------------------------------------------------------

def test_disjoint_date_ranges_read_disjoint_simulation_data():
    """The whole point of the fix. Two backtests with non-overlapping
    [start, end] windows must iterate over different bars."""
    full = _synthetic_full_df(start="2023-01-01", days=600)
    backtester._symbol_cache["AAA"] = {
        "data": full, "ts": __import__("time").time(),
    }
    try:
        win1 = backtester._fetch_yf_history_range(
            "AAA",
            start_date="2024-01-01",
            end_date="2024-06-30",
            warmup_days=30,
        )
        win2 = backtester._fetch_yf_history_range(
            "AAA",
            start_date="2024-09-01",
            end_date="2024-12-31",
            warmup_days=30,
        )
    finally:
        backtester._symbol_cache.pop("AAA", None)

    assert win1 is not None and win2 is not None
    # Define "simulation bars" as those at or after start_date
    sim1 = win1[win1.index >= pd.Timestamp("2024-01-01")]
    sim2 = win2[win2.index >= pd.Timestamp("2024-09-01")]

    # Disjoint sets — no overlap in the simulation bars
    overlap = sim1.index.intersection(sim2.index)
    assert len(overlap) == 0, (
        f"Simulation bars overlap between supposedly disjoint windows. "
        f"This is the leakage signature: walk-forward folds and "
        f"in-sample/OOS pairs must read disjoint data. Overlap count: "
        f"{len(overlap)}"
    )


# ---------------------------------------------------------------------------
# Backwards compat: legacy days= still works
# ---------------------------------------------------------------------------

def test_legacy_days_path_still_works():
    """Existing callers passing `days=` (without start_date/end_date)
    must still get the today-relative behavior. Wave 1 changes the
    foundation; it doesn't break legacy callers until they migrate."""
    sig = inspect.signature(backtester.backtest_strategy)
    days_param = sig.parameters.get("days")
    assert days_param is not None, "days= must remain accepted (legacy)"
    # And it has to come before the new date-range params in the
    # signature so positional callers don't break.
    param_order = list(sig.parameters.keys())
    assert param_order.index("days") < param_order.index("start_date"), (
        "days must remain ahead of start_date in the parameter order "
        "to preserve positional-call backward compatibility."
    )
