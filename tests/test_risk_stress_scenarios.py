"""Item 2a — historical stress scenario tests."""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture
def fake_factor_data():
    """A synthetic 'historical' factor return matrix the scenario tests
    can pretend came from Alpaca / Ken French."""
    rng = np.random.default_rng(42)
    dates = pd.bdate_range("2008-09-01", periods=44)   # ~2 months
    # Crash-like: market down hard, sectors mixed, Mom blowup
    df = pd.DataFrame({
        "sector_tech":      rng.normal(-0.012, 0.030, 44),
        "sector_financials": rng.normal(-0.030, 0.045, 44),
        "Mkt-RF":           rng.normal(-0.020, 0.032, 44),
        "SMB":              rng.normal(0.001, 0.012, 44),
    }, index=dates)
    return df


@pytest.fixture
def fake_exposures():
    """Three-symbol portfolio with known exposures."""
    return {
        "AAPL": {
            "alpha": 0,
            "beta": {"sector_tech": 1.1, "sector_financials": 0.0,
                     "Mkt-RF": 0.95, "SMB": -0.1},
            "idio_var": 0.00012, "n_obs": 252, "r_squared": 0.65,
        },
        "JPM": {
            "alpha": 0,
            "beta": {"sector_tech": 0.05, "sector_financials": 1.3,
                     "Mkt-RF": 1.05, "SMB": 0.15},
            "idio_var": 0.00018, "n_obs": 252, "r_squared": 0.70,
        },
        "IWM": {
            "alpha": 0,
            "beta": {"sector_tech": 0.10, "sector_financials": 0.20,
                     "Mkt-RF": 1.0, "SMB": 0.95},
            "idio_var": 0.00010, "n_obs": 252, "r_squared": 0.85,
        },
    }


# ---------------------------------------------------------------------------
# replay_scenario
# ---------------------------------------------------------------------------

class TestReplayScenario:
    def test_lehman_window_projects_loss(
        self, fake_factor_data, fake_exposures,
    ):
        """Lehman scenario with crash-like factor returns + long-only
        portfolio should project a loss."""
        from risk_stress_scenarios import (
            replay_scenario, SCENARIOS,
        )
        sc = next(s for s in SCENARIOS if s.name == "2008_lehman")
        weights = {"AAPL": 0.4, "JPM": 0.3, "IWM": 0.3}
        with patch("risk_stress_scenarios._fetch_scenario_factor_returns",
                    return_value=fake_factor_data):
            r = replay_scenario(sc, weights, fake_exposures, portfolio_value=100_000)
        assert r is not None
        assert r["scenario"] == "2008_lehman"
        assert r["total_pnl_pct"] < 0   # long book in a crash
        assert r["worst_day_pct"] < 0
        assert r["max_drawdown_pct"] <= r["total_pnl_pct"]
        assert r["n_days"] == len(fake_factor_data)
        assert r["worst_day_dollars"] == pytest.approx(
            r["worst_day_pct"] * 100_000,
        )

    def test_short_book_projects_gain(
        self, fake_factor_data, fake_exposures,
    ):
        from risk_stress_scenarios import replay_scenario, SCENARIOS
        sc = next(s for s in SCENARIOS if s.name == "2008_lehman")
        # Net short book → crash should print
        weights = {"AAPL": -0.4, "JPM": -0.3, "IWM": -0.3}
        with patch("risk_stress_scenarios._fetch_scenario_factor_returns",
                    return_value=fake_factor_data):
            r = replay_scenario(sc, weights, fake_exposures, portfolio_value=100_000)
        assert r["total_pnl_pct"] > 0

    def test_returns_none_when_no_factor_overlap(
        self, fake_exposures,
    ):
        from risk_stress_scenarios import replay_scenario, SCENARIOS
        sc = next(s for s in SCENARIOS if s.name == "2008_lehman")
        # Empty factor returns — nothing to project against
        with patch("risk_stress_scenarios._fetch_scenario_factor_returns",
                    return_value=pd.DataFrame()):
            r = replay_scenario(sc, {"AAPL": 1.0}, fake_exposures, 1_000)
        assert r is None

    def test_reports_missing_factors(
        self, fake_factor_data, fake_exposures,
    ):
        """If the live exposures reference factors that aren't in the
        scenario window, they show up in factors_missing."""
        from risk_stress_scenarios import replay_scenario, SCENARIOS
        sc = next(s for s in SCENARIOS if s.name == "1987_blackmonday")
        # Drop all sector ETFs from fake data — only French factors remain
        fr_only = fake_factor_data[["Mkt-RF", "SMB"]]
        with patch("risk_stress_scenarios._fetch_scenario_factor_returns",
                    return_value=fr_only):
            r = replay_scenario(sc, {"AAPL": 1.0}, fake_exposures, 100_000)
        assert r is not None
        assert "sector_tech" in r["factors_missing"]
        assert r["approximation_quality"] in ("medium", "low")

    def test_idio_band_present(
        self, fake_factor_data, fake_exposures,
    ):
        from risk_stress_scenarios import replay_scenario, SCENARIOS
        sc = next(s for s in SCENARIOS if s.name == "2008_lehman")
        with patch("risk_stress_scenarios._fetch_scenario_factor_returns",
                    return_value=fake_factor_data):
            r = replay_scenario(
                sc, {"AAPL": 0.5, "JPM": 0.5}, fake_exposures, 100_000,
            )
        assert r["idio_band_pct"] > 0
        assert r["idio_band_dollars"] > 0


# ---------------------------------------------------------------------------
# run_all_scenarios
# ---------------------------------------------------------------------------

class TestRunAllScenarios:
    def test_returns_results_sorted_worst_first(
        self, fake_factor_data, fake_exposures,
    ):
        from risk_stress_scenarios import run_all_scenarios, SCENARIOS
        with patch("risk_stress_scenarios._fetch_scenario_factor_returns",
                    return_value=fake_factor_data):
            results = run_all_scenarios(
                {"AAPL": 0.5, "JPM": 0.5}, fake_exposures, 100_000,
            )
        # All scenarios should return data with our fake fetcher
        assert len(results) == len(SCENARIOS)
        # Sorted ascending by total_pnl_pct (worst first)
        pnls = [r["total_pnl_pct"] for r in results]
        assert pnls == sorted(pnls)


# ---------------------------------------------------------------------------
# render_scenarios_for_prompt
# ---------------------------------------------------------------------------

class TestRenderScenariosForPrompt:
    def test_empty_returns_empty_string(self):
        from risk_stress_scenarios import render_scenarios_for_prompt
        assert render_scenarios_for_prompt([]) == ""

    def test_renders_each_scenario(
        self, fake_factor_data, fake_exposures,
    ):
        from risk_stress_scenarios import (
            run_all_scenarios, render_scenarios_for_prompt,
        )
        with patch("risk_stress_scenarios._fetch_scenario_factor_returns",
                    return_value=fake_factor_data):
            results = run_all_scenarios(
                {"AAPL": 1.0}, fake_exposures, 100_000,
            )
        rendered = render_scenarios_for_prompt(results)
        for r in results:
            assert r["scenario"] in rendered

    def test_marks_low_quality_approximations(
        self, fake_factor_data, fake_exposures,
    ):
        from risk_stress_scenarios import (
            replay_scenario, render_scenarios_for_prompt, SCENARIOS,
        )
        sc = next(s for s in SCENARIOS if s.name == "1987_blackmonday")
        # Only the SMB factor available — many factors missing
        partial = fake_factor_data[["SMB"]]
        with patch("risk_stress_scenarios._fetch_scenario_factor_returns",
                    return_value=partial):
            r = replay_scenario(sc, {"AAPL": 1.0}, fake_exposures, 100_000)
        rendered = render_scenarios_for_prompt([r])
        assert "approx" in rendered.lower()
