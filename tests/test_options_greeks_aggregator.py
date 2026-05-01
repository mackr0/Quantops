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


# ---------------------------------------------------------------------------
# Phase A2 — Greeks exposure gates
# ---------------------------------------------------------------------------

from unittest.mock import MagicMock, patch  # noqa: E402


class TestGreeksGates:
    def _ctx(self, **overrides):
        ctx = MagicMock()
        ctx.max_net_options_delta_pct = overrides.get(
            "max_net_options_delta_pct", 0.05)
        ctx.max_theta_burn_dollars_per_day = overrides.get(
            "max_theta_burn_dollars_per_day", 50.0)
        ctx.max_short_vega_dollars = overrides.get(
            "max_short_vega_dollars", 500.0)
        ctx.initial_capital = overrides.get("initial_capital", 100000)
        return ctx

    def _empty_book(self):
        from options_greeks_aggregator import compute_book_greeks
        return compute_book_greeks([])

    def test_no_proposal_empty_book_passes(self):
        from options_greeks_aggregator import check_greeks_gates
        with patch("client.get_account_info",
                   return_value={"equity": 100000}):
            result = check_greeks_gates(self._empty_book(), None,
                                          self._ctx())
        assert result["allowed"] is True
        assert result["reasons"] == []

    def test_delta_cap_blocks_excessive_directional(self):
        """Proposed contribution adds 200 share-eq delta. With equity
        $100k and limit 5% (=$5000), the dollar proxy is 200*100=$20k
        > $5k. Block."""
        from options_greeks_aggregator import check_greeks_gates
        proposed = {"delta": 200.0, "gamma": 1.0, "vega": 100.0,
                    "theta": -10.0}
        with patch("client.get_account_info",
                   return_value={"equity": 100000}):
            result = check_greeks_gates(
                self._empty_book(), proposed,
                self._ctx(max_net_options_delta_pct=0.05),
            )
        assert result["allowed"] is False
        assert any("delta" in r.lower() for r in result["reasons"])

    def test_delta_cap_passes_within_limit(self):
        from options_greeks_aggregator import check_greeks_gates
        proposed = {"delta": 25.0, "theta": -5.0, "vega": 50.0}
        with patch("client.get_account_info",
                   return_value={"equity": 100000}):
            result = check_greeks_gates(
                self._empty_book(), proposed,
                self._ctx(max_net_options_delta_pct=0.05),
            )
        # 25 * 100 = $2500 < 5% × $100k = $5000 → pass
        assert result["allowed"] is True

    def test_theta_burn_blocks_long_premium_above_cap(self):
        """Proposed contribution adds -100 theta/day. With cap of $50/day
        block."""
        from options_greeks_aggregator import check_greeks_gates
        proposed = {"delta": 10.0, "theta": -100.0, "vega": 200.0}
        with patch("client.get_account_info",
                   return_value={"equity": 100000}):
            result = check_greeks_gates(
                self._empty_book(), proposed,
                self._ctx(max_theta_burn_dollars_per_day=50.0),
            )
        assert result["allowed"] is False
        assert any("theta" in r.lower() for r in result["reasons"])

    def test_theta_burn_passes_short_premium(self):
        """Short premium contributes positive theta; should always pass
        the theta gate regardless of cap."""
        from options_greeks_aggregator import check_greeks_gates
        proposed = {"delta": -5.0, "theta": +30.0, "vega": -100.0}
        with patch("client.get_account_info",
                   return_value={"equity": 100000}):
            result = check_greeks_gates(
                self._empty_book(), proposed,
                self._ctx(max_theta_burn_dollars_per_day=50.0,
                            max_short_vega_dollars=10000),
            )
        assert "theta" not in str(result["reasons"]).lower()

    def test_short_vega_cap_blocks_excessive_short_premium(self):
        """Proposed adds -800 vega. With cap of $500 block."""
        from options_greeks_aggregator import check_greeks_gates
        proposed = {"delta": -2.0, "theta": +50.0, "vega": -800.0}
        with patch("client.get_account_info",
                   return_value={"equity": 100000}):
            result = check_greeks_gates(
                self._empty_book(), proposed,
                self._ctx(max_short_vega_dollars=500.0),
            )
        assert result["allowed"] is False
        assert any("vega" in r.lower() for r in result["reasons"])

    def test_short_vega_cap_passes_long_premium(self):
        from options_greeks_aggregator import check_greeks_gates
        proposed = {"delta": 5.0, "theta": -10.0, "vega": +200.0}
        with patch("client.get_account_info",
                   return_value={"equity": 100000}):
            result = check_greeks_gates(
                self._empty_book(), proposed,
                self._ctx(max_short_vega_dollars=500.0),
            )
        assert "vega" not in str(result["reasons"]).lower()

    def test_none_limit_disables_gate(self):
        """Setting a gate to None should disable it (no-op)."""
        from options_greeks_aggregator import check_greeks_gates
        # Massively over limit — but with all gates disabled, should pass
        proposed = {"delta": 1000.0, "theta": -500.0, "vega": -5000.0}
        with patch("client.get_account_info",
                   return_value={"equity": 100000}):
            result = check_greeks_gates(
                self._empty_book(), proposed,
                self._ctx(
                    max_net_options_delta_pct=None,
                    max_theta_burn_dollars_per_day=None,
                    max_short_vega_dollars=None,
                ),
            )
        assert result["allowed"] is True
        assert result["reasons"] == []

    def test_gate_uses_pre_book_plus_proposal(self):
        """Gate logic adds proposed contribution to existing book.
        Empty proposal but existing book over-limit should still block."""
        from options_greeks_aggregator import check_greeks_gates
        # Synthesize a book summary that's already over the delta cap
        book = {
            "options_delta": 200.0, "net_theta": -10.0, "net_vega": 50.0,
        }
        with patch("client.get_account_info",
                   return_value={"equity": 100000}):
            result = check_greeks_gates(
                book, None,  # no proposal
                self._ctx(max_net_options_delta_pct=0.05),
            )
        # Already over: 200*100=$20k > 5%*$100k=$5k
        assert result["allowed"] is False

    def test_returns_post_trade_metrics(self):
        from options_greeks_aggregator import check_greeks_gates
        book = {"options_delta": 50.0, "net_theta": -20.0, "net_vega": 100.0}
        proposed = {"delta": 30.0, "theta": -10.0, "vega": 50.0}
        with patch("client.get_account_info",
                   return_value={"equity": 100000}):
            result = check_greeks_gates(
                book, proposed, self._ctx(),
            )
        assert result["post_trade_options_delta"] == 80.0
        assert result["post_trade_theta"] == -30.0
        assert result["post_trade_vega"] == 150.0
