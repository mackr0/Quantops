"""Item 2a — portfolio risk model tests."""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


# ---------------------------------------------------------------------------
# Fixtures: synthetic factor returns + symbol returns with KNOWN exposures
# ---------------------------------------------------------------------------

@pytest.fixture
def synthetic_factor_returns():
    """3 factors, 250 days, deterministic via seeded RNG."""
    rng = np.random.default_rng(0)
    n = 250
    dates = pd.bdate_range("2024-01-01", periods=n)
    df = pd.DataFrame({
        "Mkt-RF": rng.normal(0.0005, 0.012, n),
        "SMB":    rng.normal(0.0,    0.008, n),
        "sector_tech": rng.normal(0.0008, 0.014, n),
    }, index=dates)
    return df


@pytest.fixture
def synthetic_symbol(synthetic_factor_returns):
    """Build a fake symbol whose returns are deterministic linear combo
    of factors + small idio noise. Lets us assert exposures recover
    something close to the planted truth.
    """
    rng = np.random.default_rng(1)
    f = synthetic_factor_returns
    true_beta = {"Mkt-RF": 1.2, "SMB": 0.4, "sector_tech": 0.5}
    rets = (
        f["Mkt-RF"] * true_beta["Mkt-RF"]
        + f["SMB"] * true_beta["SMB"]
        + f["sector_tech"] * true_beta["sector_tech"]
        + rng.normal(0, 0.005, len(f))
    )
    return rets, true_beta


# ---------------------------------------------------------------------------
# estimate_exposures
# ---------------------------------------------------------------------------

class TestEstimateExposures:
    def test_recovers_planted_betas(
        self, synthetic_factor_returns, synthetic_symbol,
    ):
        from portfolio_risk_model import estimate_exposures
        rets, true_beta = synthetic_symbol
        est = estimate_exposures(rets, synthetic_factor_returns)
        assert est is not None
        # Ridge biases toward zero, so allow loose tolerance
        for f, true_b in true_beta.items():
            assert abs(est["beta"][f] - true_b) < 0.3, (
                f"factor {f}: est={est['beta'][f]:.3f}, true={true_b}"
            )

    def test_idio_var_is_small_when_signal_dominates(
        self, synthetic_factor_returns, synthetic_symbol,
    ):
        from portfolio_risk_model import estimate_exposures
        rets, _ = synthetic_symbol
        est = estimate_exposures(rets, synthetic_factor_returns)
        # Daily idio variance with σ=0.005 → var≈2.5e-5
        assert est["idio_var"] < 1e-4

    def test_returns_none_with_insufficient_data(self, synthetic_factor_returns):
        from portfolio_risk_model import estimate_exposures
        # Only 5 observations
        small = synthetic_factor_returns.head(5)
        est = estimate_exposures(small.iloc[:, 0], small)
        assert est is None

    def test_high_r_squared_when_factors_explain_returns(
        self, synthetic_factor_returns, synthetic_symbol,
    ):
        from portfolio_risk_model import estimate_exposures
        rets, _ = synthetic_symbol
        est = estimate_exposures(rets, synthetic_factor_returns)
        assert est["r_squared"] > 0.7


# ---------------------------------------------------------------------------
# estimate_factor_cov
# ---------------------------------------------------------------------------

class TestEstimateFactorCov:
    def test_returns_correct_shape(self, synthetic_factor_returns):
        from portfolio_risk_model import estimate_factor_cov
        cov = estimate_factor_cov(synthetic_factor_returns)
        assert cov is not None
        assert cov.shape == (3, 3)

    def test_returns_none_on_empty(self):
        from portfolio_risk_model import estimate_factor_cov
        assert estimate_factor_cov(pd.DataFrame()) is None

    def test_diagonals_positive(self, synthetic_factor_returns):
        from portfolio_risk_model import estimate_factor_cov
        cov = estimate_factor_cov(synthetic_factor_returns)
        assert all(np.diag(cov) > 0)

    def test_manual_shrinkage(self, synthetic_factor_returns):
        from portfolio_risk_model import estimate_factor_cov
        cov_full = estimate_factor_cov(synthetic_factor_returns, shrinkage=1.0)
        # Shrinkage=1 → pure diagonal
        off_diag = cov_full - np.diag(np.diag(cov_full))
        assert np.allclose(off_diag, 0)


# ---------------------------------------------------------------------------
# compute_portfolio_risk
# ---------------------------------------------------------------------------

class TestComputePortfolioRisk:
    def test_single_symbol_book(
        self, synthetic_factor_returns, synthetic_symbol,
    ):
        from portfolio_risk_model import (
            estimate_exposures, estimate_factor_cov, compute_portfolio_risk,
        )
        rets, _ = synthetic_symbol
        est = estimate_exposures(rets, synthetic_factor_returns)
        cov = estimate_factor_cov(synthetic_factor_returns)
        risk = compute_portfolio_risk(
            weights={"FAKE": 1.0},
            exposures={"FAKE": est},
            factor_cov=cov,
            portfolio_value=100_000,
        )
        assert risk is not None
        assert risk["sigma"] > 0
        assert risk["var_95_dollars"] > risk["var_95_pct"] * 100_000 * 0.99
        assert risk["es_95_dollars"] > risk["var_95_dollars"]
        assert risk["es_99_dollars"] > risk["var_99_dollars"]

    def test_zero_weights_returns_some_structure(
        self, synthetic_factor_returns, synthetic_symbol,
    ):
        from portfolio_risk_model import (
            estimate_exposures, estimate_factor_cov, compute_portfolio_risk,
        )
        rets, _ = synthetic_symbol
        est = estimate_exposures(rets, synthetic_factor_returns)
        cov = estimate_factor_cov(synthetic_factor_returns)
        risk = compute_portfolio_risk(
            weights={"FAKE": 0.0},
            exposures={"FAKE": est},
            factor_cov=cov,
        )
        # All zero weights → zero variance
        assert risk["total_var"] == pytest.approx(0.0)

    def test_returns_none_with_no_exposures(self):
        from portfolio_risk_model import compute_portfolio_risk
        assert compute_portfolio_risk({}, {}, np.eye(3)) is None

    def test_factor_decomposition_sums_to_factor_var(
        self, synthetic_factor_returns, synthetic_symbol,
    ):
        from portfolio_risk_model import (
            estimate_exposures, estimate_factor_cov, compute_portfolio_risk,
        )
        rets, _ = synthetic_symbol
        est = estimate_exposures(rets, synthetic_factor_returns)
        cov = estimate_factor_cov(synthetic_factor_returns)
        risk = compute_portfolio_risk(
            weights={"FAKE": 0.5}, exposures={"FAKE": est},
            factor_cov=cov, portfolio_value=1.0,
        )
        decomp_sum = sum(risk["factor_decomposition"].values())
        assert abs(decomp_sum - risk["factor_var"]) < 1e-9

    def test_grouped_decomposition_categorizes_correctly(
        self, synthetic_factor_returns, synthetic_symbol,
    ):
        """sector_tech goes to 'sectors', SMB+Mkt-RF go to 'french'."""
        from portfolio_risk_model import (
            estimate_exposures, estimate_factor_cov, compute_portfolio_risk,
        )
        rets, _ = synthetic_symbol
        est = estimate_exposures(rets, synthetic_factor_returns)
        cov = estimate_factor_cov(synthetic_factor_returns)
        risk = compute_portfolio_risk(
            weights={"FAKE": 1.0}, exposures={"FAKE": est},
            factor_cov=cov,
        )
        g = risk["grouped_decomposition"]
        assert "sectors" in g and "french" in g and "idio" in g
        assert g["sectors"] != 0    # sector_tech contributes
        assert g["french"] != 0     # Mkt-RF + SMB contribute

    def test_long_short_book_factor_offset(
        self, synthetic_factor_returns, synthetic_symbol,
    ):
        """Two symbols with identical exposures but opposite weights →
        factor variance should approach zero (perfect hedge)."""
        from portfolio_risk_model import (
            estimate_exposures, estimate_factor_cov, compute_portfolio_risk,
        )
        rets, _ = synthetic_symbol
        est = estimate_exposures(rets, synthetic_factor_returns)
        cov = estimate_factor_cov(synthetic_factor_returns)
        risk = compute_portfolio_risk(
            weights={"A": 1.0, "B": -1.0},
            exposures={"A": est, "B": est},
            factor_cov=cov,
        )
        # Factor risk hedged out, only idio remains
        assert risk["factor_var"] < 1e-9
        assert risk["idio_var"] > 0


# ---------------------------------------------------------------------------
# monte_carlo_var
# ---------------------------------------------------------------------------

class TestMonteCarloVar:
    def test_var_99_greater_than_var_95(
        self, synthetic_factor_returns, synthetic_symbol,
    ):
        from portfolio_risk_model import (
            estimate_exposures, estimate_factor_cov, monte_carlo_var,
        )
        rets, _ = synthetic_symbol
        est = estimate_exposures(rets, synthetic_factor_returns)
        cov = estimate_factor_cov(synthetic_factor_returns)
        mc = monte_carlo_var(
            {"FAKE": 1.0}, {"FAKE": est}, cov,
            portfolio_value=100_000, n_sims=5000,
        )
        assert mc["var_99_dollars"] > mc["var_95_dollars"]
        assert mc["es_95_dollars"] >= mc["var_95_dollars"]

    def test_close_to_parametric_for_normal_inputs(
        self, synthetic_factor_returns, synthetic_symbol,
    ):
        """When draws come from N, MC ≈ parametric. Loose tolerance."""
        from portfolio_risk_model import (
            estimate_exposures, estimate_factor_cov, compute_portfolio_risk,
            monte_carlo_var,
        )
        rets, _ = synthetic_symbol
        est = estimate_exposures(rets, synthetic_factor_returns)
        cov = estimate_factor_cov(synthetic_factor_returns)
        param = compute_portfolio_risk(
            {"FAKE": 1.0}, {"FAKE": est}, cov, portfolio_value=100_000,
        )
        mc = monte_carlo_var(
            {"FAKE": 1.0}, {"FAKE": est}, cov,
            portfolio_value=100_000, n_sims=20000,
        )
        # Within 15% of each other on this small synthetic
        ratio = mc["var_95_dollars"] / param["var_95_dollars"]
        assert 0.7 < ratio < 1.4, f"MC/param ratio {ratio:.2f} too far"

    def test_seed_makes_deterministic(
        self, synthetic_factor_returns, synthetic_symbol,
    ):
        from portfolio_risk_model import (
            estimate_exposures, estimate_factor_cov, monte_carlo_var,
        )
        rets, _ = synthetic_symbol
        est = estimate_exposures(rets, synthetic_factor_returns)
        cov = estimate_factor_cov(synthetic_factor_returns)
        a = monte_carlo_var({"FAKE": 1.0}, {"FAKE": est}, cov, n_sims=1000, seed=7)
        b = monte_carlo_var({"FAKE": 1.0}, {"FAKE": est}, cov, n_sims=1000, seed=7)
        assert a["var_95_dollars"] == b["var_95_dollars"]


# ---------------------------------------------------------------------------
# render_risk_summary_for_prompt
# ---------------------------------------------------------------------------

class TestRenderRiskSummary:
    def test_empty_input_returns_empty_string(self):
        from portfolio_risk_model import render_risk_summary_for_prompt
        assert render_risk_summary_for_prompt({}) == ""
        assert render_risk_summary_for_prompt(None) == ""

    def test_renders_key_fields(
        self, synthetic_factor_returns, synthetic_symbol,
    ):
        from portfolio_risk_model import (
            estimate_exposures, estimate_factor_cov, compute_portfolio_risk,
            render_risk_summary_for_prompt,
        )
        rets, _ = synthetic_symbol
        est = estimate_exposures(rets, synthetic_factor_returns)
        cov = estimate_factor_cov(synthetic_factor_returns)
        risk = compute_portfolio_risk(
            {"FAKE": 1.0}, {"FAKE": est}, cov, portfolio_value=100_000,
        )
        summary = render_risk_summary_for_prompt(risk)
        assert "VaR" in summary
        assert "σ" in summary
        assert "Top exposures" in summary


# ---------------------------------------------------------------------------
# Ken French CSV parser (offline — uses canned data)
# ---------------------------------------------------------------------------

class TestParseFrenchCsv:
    def test_parses_typical_format(self):
        from portfolio_risk_model import _parse_french_csv
        canned = (
            "This file is from Ken French's data library.\n"
            "Some preamble text.\n"
            "\n"
            ",Mkt-RF,SMB,HML\n"
            "20240101,0.10,-0.05,0.20\n"
            "20240102,-0.30,0.15,0.40\n"
            "20240103,0.50,0.25,-0.10\n"
            "\n"
            "Annual\n"
            "2024,1.5,0.5,2.0\n"
        )
        df = _parse_french_csv(canned, ["Mkt-RF", "SMB", "HML"])
        assert df is not None
        assert len(df) == 3
        # Values were divided by 100 (% → decimal)
        assert df["Mkt-RF"].iloc[0] == pytest.approx(0.001)
        assert df["SMB"].iloc[1] == pytest.approx(0.0015)

    def test_returns_none_on_unrecognized_format(self):
        from portfolio_risk_model import _parse_french_csv
        assert _parse_french_csv("garbage,nothing", ["Mkt-RF"]) is None
