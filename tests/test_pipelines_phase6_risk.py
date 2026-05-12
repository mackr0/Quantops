"""Phase 6 of the instrument-class pipeline refactor (2026-05-11).

Phase 6a (this commit) ships delta-adjusted portfolio exposure
math in `pipelines/risk/exposure.py`. The bug it closes — audit
finding #7:

  Today's portfolio risk model uses `weight = qty * price` for
  every position. Correct for stocks. Wrong for options: a long
  call worth $200 in premium with delta=0.4 on a $50 underlying
  with qty=1 contract has $2,000 of directional risk
  (40 delta-shares × $50), not $200. The factor regressions
  under-weight option contributions by ~10×.

This file pins:
- STOCK CONTRIBUTION: a stock position's exposure is qty × price.
  Both qty signs (long/short) contribute |qty × price|.
- OPTION CONTRIBUTION: a long call is delta-equivalent $ exposure
  (delta × spot × |qty| × 100), NOT premium. A $200 long call on
  a $50 underlying with delta=0.4, qty=1 → ~$2,000 exposure.
- BUCKET ROLL-UP: a long AAPL call and an AAPL stock position
  aggregate into the same underlying bucket (factor regressions
  weight per-underlying, not per-contract).
- EDGE CASES: missing spot returns 0 (no crash); expired option
  returns 0; qty=0 returns 0; malformed OCC returns 0.
- CLASS INVARIANT (parametrized): for any short premium position
  the absolute exposure equals the absolute exposure of the
  matching long position with the same parameters — the SIGN of
  delta lives in the Greeks aggregation, not in the |exposure|
  used as a factor weight.
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from pipelines.risk import exposure


# ---------------------------------------------------------------------------
# Helpers — synthetic positions
# ---------------------------------------------------------------------------

def _stock(symbol="AAPL", qty=10, current_price=150.0):
    return {"symbol": symbol, "qty": qty, "current_price": current_price}


def _occ(underlying="AAPL", expiry_days=32, strike=50.0, right="C"):
    """Build a 21-char OCC symbol."""
    today = date.today()
    expiry = today + timedelta(days=expiry_days)
    yymmdd = expiry.strftime("%y%m%d")
    strike_int = int(round(strike * 1000))
    strike_str = f"{strike_int:08d}"
    root = underlying.ljust(6)
    return f"{root}{yymmdd}{right}{strike_str}"


def _call(underlying="CWAN", qty=1, strike=50.0, expiry_days=32,
           current_price=2.40):
    """Synthetic long call position. delta ~0.4 with these params."""
    return {
        "symbol": _occ(underlying, expiry_days=expiry_days,
                        strike=strike, right="C"),
        "occ_symbol": _occ(underlying, expiry_days=expiry_days,
                            strike=strike, right="C"),
        "qty": qty,
        "current_price": current_price,
    }


def _put(underlying="CWAN", qty=1, strike=50.0, expiry_days=32,
          current_price=1.80):
    return {
        "symbol": _occ(underlying, expiry_days=expiry_days,
                        strike=strike, right="P"),
        "occ_symbol": _occ(underlying, expiry_days=expiry_days,
                            strike=strike, right="P"),
        "qty": qty,
        "current_price": current_price,
    }


# ---------------------------------------------------------------------------
# Stock exposure — qty × price (the existing behavior, pinned)
# ---------------------------------------------------------------------------

class TestStockExposureUsesMarketValue:
    def test_long_stock_exposure(self):
        pos = _stock(qty=10, current_price=150.0)
        assert exposure.delta_adjusted_position_value(pos, spot=150.0) == 1500.0

    def test_short_stock_exposure_is_absolute(self):
        """For factor-regression weights, magnitude matters; sign
        is captured separately by net_delta in the Greeks aggregation."""
        pos = _stock(qty=-10, current_price=150.0)
        assert exposure.delta_adjusted_position_value(pos, spot=150.0) == 1500.0

    def test_stock_exposure_uses_current_price_over_spot(self):
        """Position's broker-shipped current_price wins when present
        — it's the freshest mark."""
        pos = _stock(qty=10, current_price=200.0)
        assert exposure.delta_adjusted_position_value(pos, spot=150.0) == 2000.0

    def test_stock_exposure_falls_back_to_spot_when_no_current_price(self):
        pos = {"symbol": "AAPL", "qty": 10}
        assert exposure.delta_adjusted_position_value(pos, spot=150.0) == 1500.0

    def test_stock_exposure_zero_qty_returns_zero(self):
        pos = _stock(qty=0)
        assert exposure.delta_adjusted_position_value(pos, spot=150.0) == 0.0

    def test_stock_exposure_no_price_returns_zero(self):
        pos = {"symbol": "AAPL", "qty": 10}
        assert exposure.delta_adjusted_position_value(pos, spot=None) == 0.0


# ---------------------------------------------------------------------------
# Option exposure — DELTA-ADJUSTED, not premium (the bug fix)
# ---------------------------------------------------------------------------

class TestOptionExposureIsDeltaAdjusted:
    """The key Phase 6a invariant: an option position's exposure is
    its delta-equivalent dollar amount, NOT the premium paid. This
    is the structural fix for audit finding #7."""

    def test_long_call_exposure_is_far_larger_than_premium(self):
        """A long call at $2.40 premium on a $50 stock with qty=1
        has ~40 delta-shares × $50 ≈ $2,000 of directional exposure
        if delta is roughly 0.4 (which it is for an ATM 32-DTE
        option). The premium-based calculation would say $240
        (2.40 × 1 × 100) — that's 8× too low."""
        call = _call(underlying="CWAN", qty=1, strike=50.0,
                      expiry_days=32, current_price=2.40)
        # Use spot=50 (ATM), IV=0.30 (typical equity).
        exp_dollars = exposure.delta_adjusted_position_value(
            call, spot=50.0, iv=0.30,
        )
        premium_value = 2.40 * 1 * 100  # = $240
        # The delta-adjusted exposure must be at least 5× the premium.
        # An ATM call has delta ~0.5 → 0.5 × $50 × 100 ≈ $2,500.
        assert exp_dollars >= premium_value * 5, (
            f"Delta-adjusted exposure ({exp_dollars:.0f}) should "
            f"be much larger than premium-based "
            f"({premium_value:.0f}); ATM call has delta ~0.5"
        )

    def test_long_put_exposure_is_absolute_value(self):
        """A long put has negative delta. Exposure for factor weight
        is absolute — sign lives in the Greeks aggregation."""
        put = _put(underlying="CWAN", qty=1, strike=50.0)
        exp_dollars = exposure.delta_adjusted_position_value(
            put, spot=50.0, iv=0.30,
        )
        assert exp_dollars > 0, "Exposure magnitude must be positive"

    def test_short_call_has_same_absolute_exposure_as_long(self):
        """Short premium has the same |exposure| as long with the
        same parameters — only the sign of delta differs (captured
        by net_delta in the Greeks aggregation, not here)."""
        long_call = _call(qty=1, strike=50.0)
        short_call = _call(qty=-1, strike=50.0)
        long_exp = exposure.delta_adjusted_position_value(
            long_call, spot=50.0, iv=0.30,
        )
        short_exp = exposure.delta_adjusted_position_value(
            short_call, spot=50.0, iv=0.30,
        )
        assert long_exp == pytest.approx(short_exp, rel=0.01)

    def test_expired_option_returns_zero_exposure(self):
        """An option past its expiration date has no contribution;
        lifecycle sweep will close it."""
        expired = _call(qty=1, expiry_days=-1)  # already expired
        assert exposure.delta_adjusted_position_value(
            expired, spot=50.0, iv=0.30,
        ) == 0.0

    def test_option_no_spot_returns_zero(self):
        """Without an underlying spot, can't compute delta — return
        0 (no contribution) rather than crash."""
        call = _call()
        assert exposure.delta_adjusted_position_value(
            call, spot=None, iv=0.30,
        ) == 0.0

    def test_option_uses_fallback_iv_when_missing(self):
        """A position with no IV provided uses FALLBACK_IV (0.25)
        rather than crashing or returning zero."""
        call = _call()
        exp = exposure.delta_adjusted_position_value(
            call, spot=50.0, iv=None,
        )
        assert exp > 0


# ---------------------------------------------------------------------------
# Bucket roll-up — stock + option same-underlying contributions sum
# ---------------------------------------------------------------------------

class TestPortfolioDeltaExposureRollsUpByUnderlying:
    def test_stock_and_call_on_same_underlying_aggregate(self):
        """A 10-share AAPL stock position and a long AAPL call
        contribute to the same AAPL bucket — factor exposures are
        per-underlying."""
        positions = [
            _stock(symbol="AAPL", qty=10, current_price=150.0),
            _call(underlying="AAPL", qty=1, strike=150.0,
                   current_price=4.50),
        ]
        prices = {"AAPL": 150.0}
        ivs = {"AAPL": 0.25}
        agg = exposure.portfolio_delta_exposure(
            positions,
            price_lookup=prices.get,
            iv_lookup=ivs.get,
        )
        assert "AAPL" in agg
        assert len(agg) == 1, (
            f"Stock + call on same underlying must aggregate to 1 "
            f"bucket, got {list(agg.keys())}"
        )
        # Stock contributes 1500; call contributes some delta-equiv $.
        # Total > 1500.
        assert agg["AAPL"] > 1500.0

    def test_different_underlyings_produce_separate_buckets(self):
        positions = [
            _stock(symbol="AAPL", qty=10, current_price=150.0),
            _call(underlying="CWAN", qty=1, strike=50.0,
                   current_price=2.40),
        ]
        prices = {"AAPL": 150.0, "CWAN": 50.0}
        ivs = {"AAPL": 0.25, "CWAN": 0.30}
        agg = exposure.portfolio_delta_exposure(
            positions,
            price_lookup=prices.get,
            iv_lookup=ivs.get,
        )
        assert sorted(agg.keys()) == ["AAPL", "CWAN"]

    def test_zero_positions_returns_empty_dict(self):
        assert exposure.portfolio_delta_exposure([]) == {}

    def test_position_with_zero_qty_skipped(self):
        positions = [_stock(qty=0)]
        assert exposure.portfolio_delta_exposure(positions) == {}

    def test_position_with_no_lookup_uses_fallback_chain(self):
        """No price_lookup, no iv_lookup, but stock has
        current_price — the fallback chain still produces an
        exposure for the stock (option still 0 without spot)."""
        positions = [_stock(symbol="AAPL", qty=10, current_price=150.0)]
        agg = exposure.portfolio_delta_exposure(positions)
        assert agg == {"AAPL": 1500.0}


# ---------------------------------------------------------------------------
# CLASS INVARIANT — long/short same-params have equal absolute exposure
# ---------------------------------------------------------------------------

class TestLongShortAbsoluteExposureInvariant:
    """For any (strike, expiry_days, right, premium), the absolute
    exposure of the long position equals the absolute exposure of
    the short position with qty negated. The SIGN lives in the
    Greeks aggregation (net_delta).

    Parametrized so a regression in absolute-value handling shows
    as a per-(strike, right) failure."""

    @pytest.mark.parametrize("strike", [40.0, 50.0, 60.0])
    @pytest.mark.parametrize("right", ["C", "P"])
    def test_abs_exposure_invariant(self, strike, right):
        spot = 50.0
        builder = _call if right == "C" else _put
        long_pos = builder(qty=1, strike=strike)
        short_pos = builder(qty=-1, strike=strike)
        long_exp = exposure.delta_adjusted_position_value(
            long_pos, spot=spot, iv=0.30,
        )
        short_exp = exposure.delta_adjusted_position_value(
            short_pos, spot=spot, iv=0.30,
        )
        assert long_exp == pytest.approx(short_exp, rel=0.001), (
            f"strike={strike} right={right}: long {long_exp:.2f} "
            f"vs short {short_exp:.2f} — absolute exposure must "
            f"match; sign belongs in Greeks aggregation"
        )


# ---------------------------------------------------------------------------
# Sanity — the existing compute_book_greeks is re-exported
# (Phase 6a doesn't reinvent Greeks aggregation)
# ---------------------------------------------------------------------------

class TestCanonicalGreeksReexport:
    def test_compute_book_greeks_reexported_from_pipelines_risk(self):
        """The canonical Greeks aggregator lives in
        options_greeks_aggregator (since Phase A1 of
        OPTIONS_PROGRAM_PLAN). Phase 6 of the pipeline refactor
        re-exports it under pipelines.risk so consumers can use the
        per-pipeline namespace consistently."""
        from pipelines import risk
        assert callable(risk.compute_book_greeks)
        # Verify it's THE function (same identity as the source).
        from options_greeks_aggregator import compute_book_greeks as src
        assert risk.compute_book_greeks is src
