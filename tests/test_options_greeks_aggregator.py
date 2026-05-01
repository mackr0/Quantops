"""Phase A1 of OPTIONS_PROGRAM_PLAN.md — book-level Greeks aggregator.

Verifies:
  - Stock-only books: net delta = sum of qty, options Greeks = 0
  - Single long-call: net delta > 0, gamma/vega > 0, theta < 0
  - Long stock + short call (covered call): delta partially offset
  - Iron-condor-like book: net delta near 0, net theta > 0 (collecting)
  - Expired options skipped without crashing
  - Missing IV → fallback used + counter incremented
  - Missing spot → leg skipped
  - Empty book → all-zero summary
  - Render output: empty book → empty string; non-empty → labeled lines
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _option_pos(symbol_a, expiry_str, strike, right, qty,
                  current_price=None):
    """Build an option position dict (matches client.get_positions shape
    for option positions: symbol IS the OCC string)."""
    from datetime import date as _date
    from options_trader import format_occ_symbol
    if isinstance(expiry_str, str):
        y, m, d = expiry_str.split("-")
        exp = _date(int(y), int(m), int(d))
    else:
        exp = expiry_str
    occ = format_occ_symbol(symbol_a, exp, strike, right)
    return {
        "symbol": occ,
        "occ_symbol": occ,
        "qty": qty,
        "current_price": current_price,
    }


def _stock_pos(symbol, qty):
    return {"symbol": symbol, "qty": qty}


def _future_date(days=30):
    return (date.today() + timedelta(days=days)).isoformat()


class TestStockOnlyBook:
    def test_long_stock_only_returns_qty_as_delta(self):
        from options_greeks_aggregator import compute_book_greeks
        positions = [_stock_pos("AAPL", 100), _stock_pos("MSFT", 50)]
        result = compute_book_greeks(positions)
        assert result["net_delta"] == 150
        assert result["stock_delta"] == 150
        assert result["options_delta"] == 0
        assert result["net_gamma"] == 0
        assert result["net_vega"] == 0
        assert result["net_theta"] == 0
        assert result["n_options_legs"] == 0
        assert result["n_stock_positions"] == 2

    def test_short_stock_signed_correctly(self):
        from options_greeks_aggregator import compute_book_greeks
        result = compute_book_greeks([_stock_pos("TSLA", -100)])
        assert result["net_delta"] == -100


class TestLongCall:
    def test_long_call_positive_delta_gamma_vega(self):
        from options_greeks_aggregator import compute_book_greeks
        # 1 ATM call, 30 days out, IV 25%
        positions = [_option_pos("AAPL", _future_date(30), 150.0, "C", 1)]
        result = compute_book_greeks(
            positions,
            price_lookup=lambda sym: 150.0,  # ATM
            iv_lookup=lambda sym: 0.25,
        )
        # ATM call delta ≈ 0.5 → net_delta ≈ 0.5 * 100 = ~50
        assert 40 < result["net_delta"] < 60
        assert result["net_gamma"] > 0
        assert result["net_vega"] > 0
        assert result["net_theta"] < 0  # long = decay against us
        assert result["n_options_legs"] == 1
        assert result["fallback_iv_count"] == 0

    def test_short_call_signs_invert(self):
        from options_greeks_aggregator import compute_book_greeks
        # SHORT 1 ATM call (qty = -1)
        positions = [_option_pos("AAPL", _future_date(30), 150.0, "C", -1)]
        result = compute_book_greeks(
            positions, price_lookup=lambda s: 150.0,
            iv_lookup=lambda s: 0.25,
        )
        assert result["net_delta"] < 0  # short = negative delta
        assert result["net_theta"] > 0  # short = collecting decay
        assert result["net_vega"] < 0  # short = bleeding on IV expansion


class TestCoveredCall:
    def test_covered_call_delta_partially_offset(self):
        """100 shares of AAPL + 1 short OTM call. Stock contributes
        +100 delta; short call contributes negative delta. Net should
        still be positive but less than 100."""
        from options_greeks_aggregator import compute_book_greeks
        positions = [
            _stock_pos("AAPL", 100),
            _option_pos("AAPL", _future_date(30), 160.0, "C", -1),  # 7% OTM
        ]
        result = compute_book_greeks(
            positions, price_lookup=lambda s: 150.0,
            iv_lookup=lambda s: 0.25,
        )
        # OTM call delta is ~0.2-0.3; -1 contract → -20 to -30 delta
        # 100 stock - 25 = 75 net delta
        assert 60 < result["net_delta"] < 90
        # Theta is positive (we sold premium)
        assert result["net_theta"] > 0


class TestIronCondorLikeBook:
    def test_short_strangle_near_delta_neutral(self):
        """A short strangle (sell OTM put + sell OTM call) at equal
        deltas should produce near-zero net delta and positive net
        theta (collecting premium on both sides)."""
        from options_greeks_aggregator import compute_book_greeks
        positions = [
            _option_pos("AAPL", _future_date(30), 140.0, "P", -1),  # short OTM put
            _option_pos("AAPL", _future_date(30), 160.0, "C", -1),  # short OTM call
        ]
        result = compute_book_greeks(
            positions, price_lookup=lambda s: 150.0,
            iv_lookup=lambda s: 0.25,
        )
        # Symmetric strangle → near-zero delta
        assert abs(result["net_delta"]) < 15
        # Both legs short → positive theta
        assert result["net_theta"] > 0
        # Both legs short → negative vega (bleed on vol expansion)
        assert result["net_vega"] < 0


class TestExpiredHandling:
    def test_expired_option_skipped_not_crashes(self):
        from options_greeks_aggregator import compute_book_greeks
        past = (date.today() - timedelta(days=5)).isoformat()
        positions = [_option_pos("AAPL", past, 150.0, "C", 1)]
        result = compute_book_greeks(
            positions, price_lookup=lambda s: 150.0,
            iv_lookup=lambda s: 0.25,
        )
        assert result["n_options_legs"] == 0
        assert result["expired_skipped"] == 1
        assert result["net_delta"] == 0


class TestFallbacks:
    def test_missing_iv_uses_fallback_increments_counter(self):
        from options_greeks_aggregator import compute_book_greeks, FALLBACK_IV
        positions = [_option_pos("AAPL", _future_date(30), 150.0, "C", 1)]
        result = compute_book_greeks(
            positions, price_lookup=lambda s: 150.0,
            iv_lookup=lambda s: None,  # oracle returned None
        )
        assert result["fallback_iv_count"] == 1
        # Greeks still computed via fallback IV
        assert result["n_options_legs"] == 1
        assert result["net_delta"] > 0

    def test_missing_spot_skips_leg(self):
        from options_greeks_aggregator import compute_book_greeks
        positions = [_option_pos("AAPL", _future_date(30), 150.0, "C", 1)]
        result = compute_book_greeks(
            positions, price_lookup=lambda s: None,
            iv_lookup=lambda s: 0.25,
        )
        assert result["n_options_legs"] == 0  # leg skipped, not crashed
        assert result["net_delta"] == 0

    def test_position_current_price_fallback_when_lookup_fails(self):
        """When price_lookup returns None but the position dict has a
        current_price field (Alpaca ships this), use it."""
        from options_greeks_aggregator import compute_book_greeks
        pos = _option_pos("AAPL", _future_date(30), 150.0, "C", 1,
                          current_price=150.0)
        result = compute_book_greeks(
            [pos], price_lookup=lambda s: None,
            iv_lookup=lambda s: 0.25,
        )
        assert result["n_options_legs"] == 1
        assert result["net_delta"] > 0


class TestEmptyBook:
    def test_no_positions_returns_zero_summary(self):
        from options_greeks_aggregator import compute_book_greeks
        result = compute_book_greeks([])
        assert result["net_delta"] == 0
        assert result["n_options_legs"] == 0
        assert result["n_stock_positions"] == 0
        assert result["by_leg"] == []

    def test_zero_qty_position_skipped(self):
        from options_greeks_aggregator import compute_book_greeks
        pos = _option_pos("AAPL", _future_date(30), 150.0, "C", 0)
        result = compute_book_greeks(
            [pos], price_lookup=lambda s: 150.0,
            iv_lookup=lambda s: 0.25,
        )
        assert result["n_options_legs"] == 0


class TestRender:
    def test_render_empty_book_returns_empty_string(self):
        from options_greeks_aggregator import (
            compute_book_greeks, render_greeks_for_prompt,
        )
        result = compute_book_greeks([])
        assert render_greeks_for_prompt(result) == ""

    def test_render_includes_delta_line(self):
        from options_greeks_aggregator import (
            compute_book_greeks, render_greeks_for_prompt,
        )
        result = compute_book_greeks([_stock_pos("AAPL", 100)])
        out = render_greeks_for_prompt(result)
        assert "BOOK GREEKS" in out
        assert "Delta" in out

    def test_render_with_options_includes_gamma_vega_theta(self):
        from options_greeks_aggregator import (
            compute_book_greeks, render_greeks_for_prompt,
        )
        result = compute_book_greeks(
            [_option_pos("AAPL", _future_date(30), 150.0, "C", 1)],
            price_lookup=lambda s: 150.0, iv_lookup=lambda s: 0.25,
        )
        out = render_greeks_for_prompt(result)
        assert "Gamma" in out
        assert "Vega" in out
        assert "Theta" in out

    def test_render_flags_fallback_iv_use(self):
        from options_greeks_aggregator import (
            compute_book_greeks, render_greeks_for_prompt,
        )
        result = compute_book_greeks(
            [_option_pos("AAPL", _future_date(30), 150.0, "C", 1)],
            price_lookup=lambda s: 150.0, iv_lookup=lambda s: None,
        )
        out = render_greeks_for_prompt(result)
        assert "fallback IV" in out
