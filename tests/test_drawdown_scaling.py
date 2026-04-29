"""P4.3 of LONG_SHORT_PLAN.md — drawdown capital scaling tests."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


# ---------------------------------------------------------------------------
# compute_capital_scale
# ---------------------------------------------------------------------------

def test_full_size_when_no_drawdown():
    from drawdown_scaling import compute_capital_scale
    assert compute_capital_scale(0.0) == 1.0
    assert compute_capital_scale(-1.0) == 1.0  # negative dd treated as zero
    assert compute_capital_scale(None) == 1.0


def test_breakpoints_match_schedule():
    from drawdown_scaling import compute_capital_scale
    assert compute_capital_scale(0.0) == pytest.approx(1.00, abs=1e-6)
    assert compute_capital_scale(5.0) == pytest.approx(0.85, abs=1e-6)
    assert compute_capital_scale(10.0) == pytest.approx(0.65, abs=1e-6)
    assert compute_capital_scale(15.0) == pytest.approx(0.45, abs=1e-6)
    assert compute_capital_scale(20.0) == pytest.approx(0.25, abs=1e-6)


def test_floor_at_25_percent_below_max_drawdown():
    from drawdown_scaling import compute_capital_scale
    assert compute_capital_scale(25.0) == 0.25
    assert compute_capital_scale(50.0) == 0.25
    assert compute_capital_scale(99.0) == 0.25


def test_linear_interpolation_between_breakpoints():
    """At 7.5% drawdown (midway between 5% and 10%), scale should be
    midway between 0.85 and 0.65 = 0.75."""
    from drawdown_scaling import compute_capital_scale
    assert compute_capital_scale(7.5) == pytest.approx(0.75, abs=0.001)
    # 12.5% midway between 10% and 15% → 0.55
    assert compute_capital_scale(12.5) == pytest.approx(0.55, abs=0.001)
    # 17.5% midway between 15% and 20% → 0.35
    assert compute_capital_scale(17.5) == pytest.approx(0.35, abs=0.001)


def test_monotonically_decreasing():
    """Scale must never INCREASE as drawdown grows — the safety net
    only tightens."""
    from drawdown_scaling import compute_capital_scale
    prev = 1.0
    for dd_pct in [1, 2, 3, 5, 8, 10, 12, 15, 17, 20, 25, 50]:
        scale = compute_capital_scale(dd_pct)
        assert scale <= prev, f"scale increased at dd={dd_pct}: {prev} → {scale}"
        prev = scale


# ---------------------------------------------------------------------------
# render_for_prompt
# ---------------------------------------------------------------------------

def test_render_empty_when_no_drawdown():
    from drawdown_scaling import render_for_prompt
    assert render_for_prompt(None) == ""
    assert render_for_prompt({}) == ""
    assert render_for_prompt({"drawdown_pct": 0.0}) == ""
    assert render_for_prompt({"drawdown_pct": -5.0}) == ""


def test_render_empty_at_full_scale():
    """Below the first breakpoint where scale is still ~1.0, suppress
    the block — no point telling the AI to multiply by 1.0."""
    from drawdown_scaling import render_for_prompt
    # 0.1% drawdown → scale ≈ 0.997 → still effectively 1.0, suppress
    assert render_for_prompt({"drawdown_pct": 0.05}) == ""


def test_render_includes_scale_and_drawdown():
    from drawdown_scaling import render_for_prompt
    text = render_for_prompt(
        {"drawdown_pct": 10.0, "peak_equity": 1_000_000, "current_equity": 900_000}
    )
    assert "DRAWDOWN CAPITAL SCALE" in text
    assert "0.65" in text  # scale at 10% drawdown
    assert "10.0%" in text  # drawdown shown
    assert "$1,000,000" in text or "$1,000,000" in text  # peak
    assert "$900,000" in text  # current


def test_render_at_floor():
    from drawdown_scaling import render_for_prompt
    text = render_for_prompt(
        {"drawdown_pct": 25.0, "peak_equity": 1_000_000, "current_equity": 750_000}
    )
    assert "0.25" in text
    assert "Multiply" in text
