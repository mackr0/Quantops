"""Phase 6b of the instrument-class pipeline refactor (2026-05-11).

Phase 6b WIRES the Phase 6a pure functions into production:

  1. `portfolio_risk_model.compute_portfolio_risk_from_positions`
     converts raw positions into "effective positions" via
     `pipelines.risk.exposure.effective_positions_for_risk_model`
     before running the factor regression. Option positions stop
     being silently dropped (their OCC symbols had no bars in the
     pre-refactor loop) and now contribute their delta-equivalent
     dollar exposure under the underlying ticker.

  2. `multi_scheduler` attaches `book_greeks` to the risk snapshot
     dict via `pipelines.risk.compute_book_greeks`. The renderer
     surfaces a "Greeks: Δ ... Γ ... ν ... θ ..." line whenever
     option positions are present.

This file pins:
- HELPER CORRECTNESS: signed_portfolio_delta_exposure preserves
  direction (long call positive, short call negative); same-
  underlying contributions sum.
- ROLL-UP CORRECTNESS: effective_positions_for_risk_model produces
  one synthetic position per underlying with the signed delta-
  equivalent market_value.
- PROMPT INTEGRATION: render_risk_summary_for_prompt includes
  Greeks line when book_greeks dict has options legs; omits it
  for stock-only books (back-compat).
- BACK-COMPAT: existing stock-only callers see no behavior change
  — the renderer's pre-refactor sections still appear unchanged.
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from pipelines.risk import exposure
from portfolio_risk_model import render_risk_summary_for_prompt


# ---------------------------------------------------------------------------
# Helpers — synthetic positions
# ---------------------------------------------------------------------------

def _stock(symbol="AAPL", qty=10, current_price=150.0):
    return {"symbol": symbol, "qty": qty, "current_price": current_price,
            "market_value": qty * current_price}


def _occ(underlying="AAPL", expiry_days=32, strike=50.0, right="C"):
    today = date.today()
    expiry = today + timedelta(days=expiry_days)
    yymmdd = expiry.strftime("%y%m%d")
    strike_str = f"{int(round(strike * 1000)):08d}"
    root = underlying.ljust(6)
    return f"{root}{yymmdd}{right}{strike_str}"


def _call(underlying="CWAN", qty=1, strike=50.0, expiry_days=32,
           current_price=2.40):
    sym = _occ(underlying, expiry_days=expiry_days, strike=strike, right="C")
    return {
        "symbol": sym,
        "occ_symbol": sym,
        "qty": qty,
        "current_price": current_price,
        "market_value": qty * current_price * 100,
    }


# ---------------------------------------------------------------------------
# signed_portfolio_delta_exposure — sign preservation
# ---------------------------------------------------------------------------

class TestSignedPortfolioDeltaExposure:
    def test_long_stock_is_positive(self):
        positions = [_stock(qty=10)]
        signed = exposure.signed_portfolio_delta_exposure(positions)
        assert signed["AAPL"] == pytest.approx(1500.0)

    def test_short_stock_is_negative(self):
        positions = [_stock(qty=-10)]
        signed = exposure.signed_portfolio_delta_exposure(positions)
        assert signed["AAPL"] == pytest.approx(-1500.0)

    def test_long_call_is_positive(self):
        positions = [_call(underlying="CWAN", qty=1)]
        prices = {"CWAN": 50.0}
        signed = exposure.signed_portfolio_delta_exposure(
            positions, price_lookup=prices.get,
        )
        assert "CWAN" in signed
        assert signed["CWAN"] > 0, (
            "Long call has positive delta → positive signed exposure"
        )

    def test_short_call_is_negative(self):
        positions = [_call(underlying="CWAN", qty=-1)]
        prices = {"CWAN": 50.0}
        signed = exposure.signed_portfolio_delta_exposure(
            positions, price_lookup=prices.get,
        )
        assert signed["CWAN"] < 0, (
            "Short call has negative delta (sold premium) → "
            "negative signed exposure"
        )

    def test_long_put_is_negative(self):
        # Long put has NEGATIVE delta → negative signed exposure.
        sym = _occ("CWAN", strike=50.0, right="P")
        positions = [{
            "symbol": sym, "occ_symbol": sym, "qty": 1,
            "current_price": 1.80,
        }]
        prices = {"CWAN": 50.0}
        signed = exposure.signed_portfolio_delta_exposure(
            positions, price_lookup=prices.get,
        )
        assert signed["CWAN"] < 0

    def test_long_stock_plus_short_call_partially_offset(self):
        """Covered-call-like: long 100 shares, short 1 call. Stock
        contributes +100 × $50 = +$5,000. Short call contributes
        roughly -$2,500 (delta ~0.5 × 100 × $50). Net ~+$2,500."""
        positions = [
            _stock(symbol="CWAN", qty=100, current_price=50.0),
            _call(underlying="CWAN", qty=-1, strike=50.0,
                   current_price=2.40),
        ]
        prices = {"CWAN": 50.0}
        signed = exposure.signed_portfolio_delta_exposure(
            positions, price_lookup=prices.get,
        )
        assert "CWAN" in signed
        # Combined exposure must be LESS than stock alone (covered
        # call is a partially-offset position).
        assert 0 < signed["CWAN"] < 5000.0, (
            f"Covered call should reduce net exposure below stock-"
            f"alone $5,000; got {signed['CWAN']}"
        )


# ---------------------------------------------------------------------------
# effective_positions_for_risk_model — roll-up shape
# ---------------------------------------------------------------------------

class TestEffectivePositionsForRiskModel:
    def test_single_stock_passes_through(self):
        positions = [_stock(symbol="AAPL", qty=10)]
        eff = exposure.effective_positions_for_risk_model(positions)
        assert len(eff) == 1
        assert eff[0]["symbol"] == "AAPL"
        assert eff[0]["market_value"] == pytest.approx(1500.0)
        assert eff[0]["n_legs"] == 1

    def test_stock_and_option_same_underlying_roll_up(self):
        positions = [
            _stock(symbol="CWAN", qty=10, current_price=50.0),
            _call(underlying="CWAN", qty=1, strike=50.0),
        ]
        eff = exposure.effective_positions_for_risk_model(
            positions, price_lookup={"CWAN": 50.0}.get,
        )
        # ONE entry, with combined exposure
        assert len(eff) == 1
        assert eff[0]["symbol"] == "CWAN"
        # n_legs counts source positions
        assert eff[0]["n_legs"] == 2
        # Market value > stock-alone (long call adds delta-eq)
        assert eff[0]["market_value"] > 500.0

    def test_different_underlyings_produce_separate_entries(self):
        positions = [
            _stock(symbol="AAPL", qty=10, current_price=150.0),
            _stock(symbol="CWAN", qty=20, current_price=50.0),
        ]
        eff = exposure.effective_positions_for_risk_model(positions)
        syms = sorted(p["symbol"] for p in eff)
        assert syms == ["AAPL", "CWAN"]

    def test_zero_qty_position_excluded(self):
        positions = [_stock(qty=0)]
        eff = exposure.effective_positions_for_risk_model(positions)
        assert eff == []


# ---------------------------------------------------------------------------
# render_risk_summary_for_prompt — Greeks line integration
# ---------------------------------------------------------------------------

class TestRenderRiskSummaryGreeks:
    def _base_risk(self):
        """Risk dict with the minimal fields render needs."""
        return {
            "sigma": 0.012,
            "var_95_dollars": 1500.0, "var_95_pct": 0.015,
            "es_95_dollars": 2200.0,
        }

    def test_greeks_appear_when_book_has_options(self):
        risk = self._base_risk()
        risk["book_greeks"] = {
            "n_options_legs": 2,
            "net_delta": 35.0, "net_gamma": 0.12,
            "net_vega": -200.0, "net_theta": -45.0,
        }
        out = render_risk_summary_for_prompt(risk)
        assert "Greeks:" in out
        assert "Δ=" in out
        assert "Γ=" in out
        assert "ν=" in out  # vega
        assert "θ=" in out  # theta

    def test_greeks_omitted_when_no_options_legs(self):
        risk = self._base_risk()
        risk["book_greeks"] = {
            "n_options_legs": 0,
            "net_delta": 0.0, "net_gamma": 0.0,
            "net_vega": 0.0, "net_theta": 0.0,
        }
        out = render_risk_summary_for_prompt(risk)
        assert "Greeks:" not in out, (
            "Stock-only books shouldn't show empty Greeks line"
        )

    def test_greeks_omitted_when_book_greeks_missing(self):
        """Back-compat: existing callers that don't attach
        book_greeks see no change to the rendered output."""
        risk = self._base_risk()
        out = render_risk_summary_for_prompt(risk)
        assert "Greeks:" not in out

    def test_existing_sections_still_present(self):
        """Phase 6b adds a section; doesn't remove existing ones.
        The factor-risk numbers continue to appear."""
        risk = self._base_risk()
        out = render_risk_summary_for_prompt(risk)
        assert "VaR" in out
        assert "σ" in out

    def test_greek_signs_render_correctly(self):
        risk = self._base_risk()
        risk["book_greeks"] = {
            "n_options_legs": 1,
            "net_delta": -50.0, "net_gamma": 0.0,
            "net_vega": 100.0, "net_theta": -20.0,
        }
        out = render_risk_summary_for_prompt(risk)
        # Negative delta, positive vega rendered with explicit signs
        assert "-50" in out
        assert "+100" in out
        assert "-20" in out
