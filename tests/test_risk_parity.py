"""P4.4 of LONG_SHORT_PLAN.md — risk-budget (risk-parity) sizing tests."""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


# ---------------------------------------------------------------------------
# compute_vol_scale
# ---------------------------------------------------------------------------

def test_vol_scale_returns_one_at_target_vol():
    from risk_parity import compute_vol_scale, TARGET_VOL
    assert compute_vol_scale(TARGET_VOL) == pytest.approx(1.0, abs=1e-6)


def test_vol_scale_inverse_to_vol():
    from risk_parity import compute_vol_scale
    # Half the target vol → 2× scale (capped by VOL_SCALE_MAX = 1.6)
    s_low = compute_vol_scale(0.125)
    # Target vol → 1.0
    s_mid = compute_vol_scale(0.25)
    # Double the target vol → 0.5× scale
    s_high = compute_vol_scale(0.50)
    assert s_low > s_mid > s_high


def test_vol_scale_clamped_within_bounds():
    from risk_parity import compute_vol_scale, VOL_SCALE_MIN, VOL_SCALE_MAX
    # Crazy low vol
    assert compute_vol_scale(0.001) == VOL_SCALE_MAX
    # Crazy high vol
    assert compute_vol_scale(5.0) == VOL_SCALE_MIN


def test_vol_scale_returns_one_when_vol_unknown():
    from risk_parity import compute_vol_scale
    assert compute_vol_scale(None) == 1.0
    assert compute_vol_scale(0.0) == 1.0
    assert compute_vol_scale(-0.1) == 1.0


# ---------------------------------------------------------------------------
# analyze_position_risk
# ---------------------------------------------------------------------------

def test_analyze_returns_none_on_empty_positions():
    from risk_parity import analyze_position_risk
    assert analyze_position_risk([], 1_000_000) is None
    assert analyze_position_risk(None, 1_000_000) is None


def test_analyze_returns_none_on_zero_equity():
    from risk_parity import analyze_position_risk
    pos = [{"symbol": "AAPL", "market_value": 50_000}]
    assert analyze_position_risk(pos, 0) is None


def test_analyze_flags_high_contributor():
    """One position with 4× the vol of the others should flag as
    high-contrib; the others flag normal. Tests vol-mocking path."""
    from risk_parity import analyze_position_risk

    vol_map = {"AAPL": 0.20, "MSFT": 0.20, "GOOG": 0.20, "TSLA": 0.80}

    def fake_vol(sym, days=30):
        return vol_map.get(sym)

    positions = [
        {"symbol": "AAPL", "market_value": 50_000},
        {"symbol": "MSFT", "market_value": 50_000},
        {"symbol": "GOOG", "market_value": 50_000},
        {"symbol": "TSLA", "market_value": 50_000},
    ]
    with patch("factor_data.get_realized_vol", side_effect=fake_vol):
        a = analyze_position_risk(positions, 1_000_000)

    assert a is not None
    assert "TSLA" in a["high_contributors"]
    # Everyone else equal-vol → tagged normal
    syms_normal = [r["symbol"] for r in a["contributions"] if r["tag"] == "normal"]
    assert "AAPL" in syms_normal
    # Average is biased up by TSLA but its 4× ratio still triggers.
    tsla = next(r for r in a["contributions"] if r["symbol"] == "TSLA")
    assert tsla["ratio"] >= 2.0


def test_analyze_skips_unknown_vol():
    from risk_parity import analyze_position_risk

    def fake_vol(sym, days=30):
        return None if sym == "ZZZ" else 0.25

    positions = [
        {"symbol": "AAPL", "market_value": 50_000},
        {"symbol": "MSFT", "market_value": 50_000},
        {"symbol": "ZZZ", "market_value": 50_000},
    ]
    with patch("factor_data.get_realized_vol", side_effect=fake_vol):
        a = analyze_position_risk(positions, 1_000_000)

    assert a is not None
    syms = [r["symbol"] for r in a["contributions"]]
    assert "ZZZ" not in syms
    assert "AAPL" in syms and "MSFT" in syms


def test_analyze_returns_none_when_lt_two_known_vols():
    """With only 1 position whose vol resolves, no per-name comparison
    is meaningful — return None."""
    from risk_parity import analyze_position_risk

    def fake_vol(sym, days=30):
        return 0.25 if sym == "AAPL" else None

    positions = [
        {"symbol": "AAPL", "market_value": 50_000},
        {"symbol": "ZZZ", "market_value": 50_000},
    ]
    with patch("factor_data.get_realized_vol", side_effect=fake_vol):
        assert analyze_position_risk(positions, 1_000_000) is None


def test_analyze_handles_short_positions():
    """Short positions have negative market_value but their RISK
    contribution is the absolute exposure × vol — must use abs()."""
    from risk_parity import analyze_position_risk

    def fake_vol(sym, days=30):
        return 0.25

    positions = [
        {"symbol": "AAPL", "market_value": 50_000},
        {"symbol": "TSLA", "market_value": -50_000},  # short
        {"symbol": "MSFT", "market_value": 50_000},
    ]
    with patch("factor_data.get_realized_vol", side_effect=fake_vol):
        a = analyze_position_risk(positions, 1_000_000)
    # All 3 should land at the same contribution (5% × 25%).
    contribs = {r["symbol"]: r["contribution"] for r in a["contributions"]}
    assert contribs["AAPL"] == pytest.approx(contribs["TSLA"], abs=1e-6)
    assert contribs["AAPL"] == pytest.approx(contribs["MSFT"], abs=1e-6)


# ---------------------------------------------------------------------------
# render_for_prompt
# ---------------------------------------------------------------------------

def test_render_empty_when_no_analysis():
    from risk_parity import render_for_prompt
    assert render_for_prompt(None) == ""
    assert render_for_prompt({}) == ""


def test_render_includes_sizing_rule_and_outliers():
    from risk_parity import render_for_prompt
    a = {
        "contributions": [
            {"symbol": "TSLA", "weight": 0.05, "vol": 0.80,
             "contribution": 0.04, "ratio": 4.0, "tag": "high"},
            {"symbol": "AAPL", "weight": 0.05, "vol": 0.20,
             "contribution": 0.01, "ratio": 1.0, "tag": "normal"},
            {"symbol": "MSFT", "weight": 0.05, "vol": 0.20,
             "contribution": 0.01, "ratio": 1.0, "tag": "normal"},
        ],
        "avg_contribution": 0.02,
        "high_contrib_threshold": 0.04,
        "low_contrib_threshold": 0.01,
        "high_contributors": ["TSLA"],
        "low_contributors": [],
    }
    text = render_for_prompt(a)
    assert "RISK-BUDGET" in text
    assert "TSLA" in text
    assert "OVER-CONTRIBUTING" in text
    assert "Sizing rule" in text


def test_render_suppressed_when_nothing_actionable():
    """Two positions, both normal, no outliers → suppress."""
    from risk_parity import render_for_prompt
    a = {
        "contributions": [
            {"symbol": "AAPL", "tag": "normal", "contribution": 0.01,
             "vol": 0.22, "ratio": 1.0},
            {"symbol": "MSFT", "tag": "normal", "contribution": 0.01,
             "vol": 0.22, "ratio": 1.0},
        ],
        "avg_contribution": 0.01,
        "high_contributors": [],
        "low_contributors": [],
    }
    assert render_for_prompt(a) == ""
