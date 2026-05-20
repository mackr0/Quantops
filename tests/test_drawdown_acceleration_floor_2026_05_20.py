"""Pin the absolute-magnitude floor on drawdown_acceleration
(2026-05-20).

Without the floor, post-reset days produce tiny 7-day baselines
(e.g., 0.24%) that today's normal market noise (0.53%) easily
exceeds by >2× → ALL trades blocked despite no actual risk event.
Caught on 2026-05-20 open when every one of 13 active profiles
showed `block_new_entries` from this check.

Floor: today's intraday drawdown must be ≥1.5% absolute before
the multiple-based comparison even applies.
"""
from __future__ import annotations

import os
import sys

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from intraday_risk_monitor import (  # noqa: E402
    check_drawdown_acceleration,
    DRAWDOWN_ACCEL_MIN_ABS,
    DRAWDOWN_ACCEL_MULTIPLE,
)


def test_no_alert_when_drawdown_below_absolute_floor_even_if_multiple_exceeded():
    """The exact 2026-05-20 scenario: today=0.53%, 7d=0.24%,
    multiple=2.2× — was firing block_new_entries; now must NOT fire."""
    alert = check_drawdown_acceleration(
        today_intraday_pct=0.0053,   # 0.53%
        avg_7d_intraday_pct=0.0024,  # 0.24%
    )
    assert alert is None, (
        "Post-reset noise (0.53% drawdown vs 0.24% baseline) must NOT "
        "trigger drawdown_acceleration halt — the absolute floor "
        f"({DRAWDOWN_ACCEL_MIN_ABS*100}%) prevents this false positive"
    )


def test_alert_fires_when_drawdown_above_floor_AND_multiple_exceeded():
    """Real risk event: 3% intraday drawdown, 1% baseline, multiple=3×.
    Both conditions met → must fire (critical severity at 3×)."""
    alert = check_drawdown_acceleration(
        today_intraday_pct=0.030,   # 3.0% — clearly above floor
        avg_7d_intraday_pct=0.010,  # 1.0%
    )
    assert alert is not None
    assert alert.check_name == "drawdown_acceleration"
    assert alert.severity == "critical"  # 3.0× hits the critical bar


def test_alert_fires_at_warning_severity_at_2x_with_meaningful_drawdown():
    """3% drawdown vs 1.4% baseline = 2.14× → warning + block_new_entries."""
    alert = check_drawdown_acceleration(
        today_intraday_pct=0.030,
        avg_7d_intraday_pct=0.014,
    )
    assert alert is not None
    assert alert.severity == "warning"
    assert alert.suggested_action == "block_new_entries"


def test_no_alert_when_multiple_below_threshold_even_with_meaningful_drawdown():
    """3% drawdown vs 2% baseline = 1.5× — below 2.0× threshold → no alert
    (existing behavior, the multiple check still gates)."""
    alert = check_drawdown_acceleration(
        today_intraday_pct=0.030,
        avg_7d_intraday_pct=0.020,
    )
    assert alert is None


def test_no_alert_when_baseline_is_zero_or_negative():
    """Defensive: zero/negative 7d average shouldn't divide-by-zero."""
    assert check_drawdown_acceleration(0.05, 0.0) is None
    assert check_drawdown_acceleration(0.05, -0.01) is None


def test_floor_value_is_sane():
    """The floor must be a non-trivial drawdown — at least 0.5% —
    otherwise it doesn't filter the noise it's meant to catch."""
    assert DRAWDOWN_ACCEL_MIN_ABS >= 0.005, (
        "DRAWDOWN_ACCEL_MIN_ABS is set too low to filter noise; "
        "post-reset baselines (~0.2-0.3%) need at least 0.5% floor "
        "to prevent false positives"
    )


def test_floor_constant_is_imported_at_module_level():
    """If someone removes DRAWDOWN_ACCEL_MIN_ABS, the import in this
    test breaks first — the regression surfaces immediately."""
    import intraday_risk_monitor
    assert hasattr(intraday_risk_monitor, "DRAWDOWN_ACCEL_MIN_ABS")
    assert intraday_risk_monitor.DRAWDOWN_ACCEL_MIN_ABS == DRAWDOWN_ACCEL_MIN_ABS
