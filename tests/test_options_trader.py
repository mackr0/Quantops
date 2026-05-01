"""Item 1a of COMPETITIVE_GAP_PLAN.md — options trading layer foundation.

Tests the pure-math foundation: Black-Scholes Greeks, OCC symbol
formatting, strategy primitive specs. Order submission tested via
mocked Alpaca client (live submission is verified post-deploy).
"""
from __future__ import annotations

import os
import sys
from datetime import date
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


# ---------------------------------------------------------------------------
# Black-Scholes Greeks
# ---------------------------------------------------------------------------

class TestGreeks:
    def test_atm_call_30d_25vol(self):
        """ATM 30-day call with 25% IV on $100 stock at 4.5% rate.
        Hand-computed B-S price: ~$3.04. (σ=0.25 → ~$3.04; σ=0.20 → ~$2.43.)"""
        from options_trader import compute_greeks
        g = compute_greeks(spot=100, strike=100, days_to_expiry=30,
                            iv=0.25, is_call=True, risk_free_rate=0.045)
        assert g is not None
        # Price ~$3.04 ± a few cents
        assert 2.8 < g["price"] < 3.3
        # ATM call delta ≈ 0.50-0.55 (slightly above 0.5 for positive r)
        assert 0.50 < g["delta"] < 0.60
        # Gamma positive
        assert g["gamma"] > 0
        # Theta negative for long option
        assert g["theta"] < 0
        # Vega positive
        assert g["vega"] > 0

    def test_atm_put_call_parity(self):
        """C - P = S - K * exp(-rT). Verify Greeks satisfy this within
        rounding."""
        from options_trader import compute_greeks
        from math import exp
        spot, strike, days, iv, r = 100, 100, 30, 0.25, 0.045
        c = compute_greeks(spot, strike, days, iv, True, r)
        p = compute_greeks(spot, strike, days, iv, False, r)
        T = days / 365.0
        expected_diff = spot - strike * exp(-r * T)
        actual_diff = c["price"] - p["price"]
        assert abs(actual_diff - expected_diff) < 0.01

    def test_otm_call_low_delta(self):
        """OTM call far from money has delta < 0.5."""
        from options_trader import compute_greeks
        g = compute_greeks(spot=100, strike=120, days_to_expiry=30,
                            iv=0.25, is_call=True)
        assert g["delta"] < 0.30
        assert g["price"] < 1.0  # cheap, far OTM

    def test_itm_put_negative_delta_near_one(self):
        """Deep ITM put has delta close to -1."""
        from options_trader import compute_greeks
        g = compute_greeks(spot=80, strike=100, days_to_expiry=30,
                            iv=0.25, is_call=False)
        assert g["delta"] < -0.85
        assert g["price"] > 19  # intrinsic ≈ 20

    def test_invalid_inputs_return_none(self):
        from options_trader import compute_greeks
        assert compute_greeks(0, 100, 30, 0.25) is None
        assert compute_greeks(100, 0, 30, 0.25) is None
        assert compute_greeks(100, 100, 0, 0.25) is None
        assert compute_greeks(100, 100, 30, 0) is None
        assert compute_greeks(100, 100, 30, -0.25) is None


# ---------------------------------------------------------------------------
# OCC symbol formatter
# ---------------------------------------------------------------------------

class TestOCCSymbol:
    def test_format_aapl_call(self):
        from options_trader import format_occ_symbol
        s = format_occ_symbol("AAPL", date(2025, 5, 16), 150.0, "C")
        # AAPL  250516C00150000  (6-char root padded with spaces)
        assert s.startswith("AAPL  ")
        assert s[6:12] == "250516"
        assert s[12] == "C"
        assert s[13:21] == "00150000"

    def test_format_put_decimal_strike(self):
        from options_trader import format_occ_symbol
        s = format_occ_symbol("TSLA", date(2026, 6, 19), 187.5, "P")
        assert s[6:12] == "260619"
        assert s[12] == "P"
        # 187.5 × 1000 = 187500
        assert s[13:21] == "00187500"

    def test_short_root_padded(self):
        from options_trader import format_occ_symbol
        s = format_occ_symbol("F", date(2026, 1, 16), 12.0, "C")
        # F + 5 spaces
        assert s[:6] == "F     "

    def test_lowercase_right_normalized(self):
        from options_trader import format_occ_symbol
        s = format_occ_symbol("AAPL", date(2025, 5, 16), 150.0, "c")
        assert s[12] == "C"

    def test_invalid_right_raises(self):
        from options_trader import format_occ_symbol
        with pytest.raises(ValueError):
            format_occ_symbol("AAPL", date(2025, 5, 16), 150.0, "X")

    def test_parse_round_trip(self):
        from options_trader import format_occ_symbol, parse_occ_symbol
        s = format_occ_symbol("NVDA", date(2026, 9, 18), 175.50, "P")
        parsed = parse_occ_symbol(s)
        assert parsed["underlying"] == "NVDA"
        assert parsed["expiry"] == date(2026, 9, 18)
        assert parsed["strike"] == 175.50
        assert parsed["right"] == "P"


# ---------------------------------------------------------------------------
# Strategy primitives
# ---------------------------------------------------------------------------

class TestLongPut:
    def test_basic_spec(self):
        from options_trader import build_long_put
        spec = build_long_put("AAPL", date(2025, 5, 16), 145.0, qty=2,
                                spot_price=150.0)
        assert spec["strategy"] == "long_put"
        assert spec["right"] == "P"
        assert spec["side"] == "buy"
        assert spec["qty"] == 2
        assert "AAPL" in spec["occ_symbol"]
        # 145 / 150 - 1 = -3.33%
        assert spec["moneyness_pct"] == pytest.approx(-3.33, abs=0.1)

    def test_zero_qty_raises(self):
        from options_trader import build_long_put
        with pytest.raises(ValueError):
            build_long_put("AAPL", date(2025, 5, 16), 145.0, qty=0)


class TestCoveredCall:
    def test_qty_derived_from_shares(self):
        """1 contract per 100 shares — 250 shares → 2 contracts (rounding down)."""
        from options_trader import build_covered_call
        spec = build_covered_call("AAPL", date(2025, 5, 16), 160.0,
                                    shares_held=250, spot_price=150.0)
        assert spec["qty"] == 2  # 250 // 100
        assert spec["shares_covered"] == 200
        assert spec["side"] == "sell"
        assert spec["right"] == "C"

    def test_below_100_shares_raises(self):
        from options_trader import build_covered_call
        with pytest.raises(ValueError):
            build_covered_call("AAPL", date(2025, 5, 16), 160.0,
                                 shares_held=50)

    def test_capped_upside_computed(self):
        from options_trader import build_covered_call
        spec = build_covered_call("AAPL", date(2025, 5, 16), 160.0,
                                    shares_held=100, spot_price=150.0)
        # If assigned, upside per share = strike - spot = $10
        assert spec["max_capped_upside_per_share"] == 10.0


class TestCashSecuredPut:
    def test_cash_required(self):
        """Cash to cover = strike × 100 × qty."""
        from options_trader import build_cash_secured_put
        spec = build_cash_secured_put("AAPL", date(2025, 5, 16), 140.0,
                                        qty=3, spot_price=150.0)
        assert spec["cash_required"] == 140.0 * 100 * 3  # 42_000
        assert spec["side"] == "sell"


class TestLongCall:
    def test_basic_spec(self):
        from options_trader import build_long_call
        spec = build_long_call("NVDA", date(2026, 6, 19), 200.0, qty=1,
                                 spot_price=175.0)
        assert spec["strategy"] == "long_call"
        assert spec["right"] == "C"
        assert spec["side"] == "buy"


# ---------------------------------------------------------------------------
# Order submission (mocked)
# ---------------------------------------------------------------------------

class TestOrderSubmission:
    def test_market_order_submitted_with_correct_kwargs(self):
        from options_trader import submit_option_order
        api = MagicMock()
        api.submit_order.return_value = MagicMock(id="opt-order-123")
        order_id = submit_option_order(
            api, "AAPL  250516C00150000", side="buy", qty=2,
            order_type="market",
        )
        assert order_id == "opt-order-123"
        kwargs = api.submit_order.call_args.kwargs
        assert kwargs["symbol"] == "AAPL  250516C00150000"
        assert kwargs["qty"] == 2
        assert kwargs["side"] == "buy"
        assert kwargs["type"] == "market"
        assert kwargs["time_in_force"] == "day"
        assert "limit_price" not in kwargs

    def test_limit_order_includes_limit_price(self):
        from options_trader import submit_option_order
        api = MagicMock()
        api.submit_order.return_value = MagicMock(id="opt-order-456")
        order_id = submit_option_order(
            api, "AAPL  250516C00150000", side="buy", qty=1,
            order_type="limit", limit_price=2.55,
        )
        assert order_id == "opt-order-456"
        kwargs = api.submit_order.call_args.kwargs
        assert kwargs["type"] == "limit"
        assert kwargs["limit_price"] == 2.55

    def test_limit_order_without_price_returns_none(self):
        """Defensive: limit order without limit_price is invalid."""
        from options_trader import submit_option_order
        api = MagicMock()
        order_id = submit_option_order(
            api, "AAPL  250516C00150000", side="buy", qty=1,
            order_type="limit", limit_price=None,
        )
        assert order_id is None
        api.submit_order.assert_not_called()

    def test_invalid_side_returns_none(self):
        from options_trader import submit_option_order
        api = MagicMock()
        assert submit_option_order(api, "X", side="hold", qty=1) is None
        api.submit_order.assert_not_called()

    def test_broker_failure_returns_none_not_raises(self):
        """Failure is logged, not raised — caller decides handling."""
        from options_trader import submit_option_order
        api = MagicMock()
        api.submit_order.side_effect = Exception("alpaca rejected")
        order_id = submit_option_order(api, "AAPL  250516C00150000",
                                          side="buy", qty=1)
        assert order_id is None


# ---------------------------------------------------------------------------
# execute_option_strategy — AI proposal → broker submission
# ---------------------------------------------------------------------------

class TestExecuteOptionStrategy:
    """Item 1a executor — wires AI proposals into broker calls.

    Constraints under test:
      - covered_call/protective_put need ≥100 shares
      - cash_secured_put needs buying power ≥ strike × 100 × contracts
      - long_call/long_put premium ≤ 1% of equity
      - Invalid strategy / missing fields / past expiry → SKIP
    """

    def _ctx(self, db_path=None):
        ctx = MagicMock()
        ctx.db_path = db_path
        return ctx

    def _patch_account_state(self, monkeypatch, positions, account):
        # client.get_positions / get_account_info are imported inside
        # the function — patch them on the client module.
        import client
        monkeypatch.setattr(client, "get_positions",
                            lambda ctx=None: positions)
        monkeypatch.setattr(client, "get_account_info",
                            lambda ctx=None: account)

    def test_invalid_strategy_returns_skip(self, monkeypatch):
        from options_trader import execute_option_strategy
        self._patch_account_state(monkeypatch, [], {"equity": 100000})
        api = MagicMock()
        result = execute_option_strategy(api, {
            "option_strategy": "iron_condor",  # not supported
            "symbol": "AAPL", "strike": 150, "expiry": "2099-01-01",
            "contracts": 1,
        }, ctx=self._ctx(), log=False)
        assert result["action"] == "SKIP"
        assert "Unsupported" in result["reason"]
        api.submit_order.assert_not_called()

    def test_missing_fields_returns_skip(self, monkeypatch):
        from options_trader import execute_option_strategy
        self._patch_account_state(monkeypatch, [], {"equity": 100000})
        api = MagicMock()
        result = execute_option_strategy(api, {
            "option_strategy": "long_call",
            "symbol": "AAPL",
            # missing strike / expiry / contracts
        }, ctx=self._ctx(), log=False)
        assert result["action"] == "SKIP"
        api.submit_order.assert_not_called()

    def test_past_expiry_returns_skip(self, monkeypatch):
        from options_trader import execute_option_strategy
        self._patch_account_state(monkeypatch, [], {"equity": 100000})
        api = MagicMock()
        result = execute_option_strategy(api, {
            "option_strategy": "long_call", "symbol": "AAPL",
            "strike": 150, "expiry": "2020-01-01", "contracts": 1,
            "limit_price": 1.00,
        }, ctx=self._ctx(), log=False)
        assert result["action"] == "SKIP"
        assert "future" in result["reason"]

    def test_covered_call_without_100_shares_skips(self, monkeypatch):
        from options_trader import execute_option_strategy
        self._patch_account_state(monkeypatch,
            [{"symbol": "AAPL", "qty": "50"}],  # only 50 shares
            {"equity": 100000})
        api = MagicMock()
        result = execute_option_strategy(api, {
            "option_strategy": "covered_call", "symbol": "AAPL",
            "strike": 175, "expiry": "2099-01-01", "contracts": 1,
        }, ctx=self._ctx(), log=False)
        assert result["action"] == "SKIP"
        assert "100 shares" in result["reason"]

    def test_csp_exceeding_buying_power_skips(self, monkeypatch):
        from options_trader import execute_option_strategy
        self._patch_account_state(monkeypatch, [],
            {"equity": 5000, "buying_power": 5000})
        api = MagicMock()
        # 10 contracts × $200 strike × 100 = $200,000 — way over
        result = execute_option_strategy(api, {
            "option_strategy": "cash_secured_put", "symbol": "AAPL",
            "strike": 200, "expiry": "2099-01-01", "contracts": 10,
        }, ctx=self._ctx(), log=False)
        assert result["action"] == "SKIP"
        assert "buying power" in result["reason"]

    def test_long_call_premium_over_1pct_skips(self, monkeypatch):
        from options_trader import execute_option_strategy
        self._patch_account_state(monkeypatch, [],
            {"equity": 10000, "buying_power": 10000})
        api = MagicMock()
        # 5 contracts × $5 premium × 100 = $2,500 = 25% of equity
        result = execute_option_strategy(api, {
            "option_strategy": "long_call", "symbol": "AAPL",
            "strike": 150, "expiry": "2099-01-01", "contracts": 5,
            "limit_price": 5.00,
        }, ctx=self._ctx(), log=False)
        assert result["action"] == "SKIP"
        assert "1%" in result["reason"]

    def test_successful_long_call_returns_options_open(self, monkeypatch):
        from options_trader import execute_option_strategy
        self._patch_account_state(monkeypatch, [],
            {"equity": 100000, "buying_power": 100000})
        api = MagicMock()
        api.submit_order.return_value = MagicMock(id="opt-1234")
        result = execute_option_strategy(api, {
            "option_strategy": "long_call", "symbol": "AAPL",
            "strike": 150, "expiry": "2099-01-01", "contracts": 1,
            "limit_price": 2.55, "confidence": 65,
            "reasoning": "test setup",
        }, ctx=self._ctx(), log=False)
        assert result["action"] == "OPTIONS_OPEN"
        assert result["order_id"] == "opt-1234"
        assert result["option_strategy"] == "long_call"
        assert result["expiry"] == "2099-01-01"
        assert result["strike"] == 150.0
        # Submitted with correct OCC + side
        kwargs = api.submit_order.call_args.kwargs
        assert kwargs["side"] == "buy"
        assert kwargs["qty"] == 1
        assert kwargs["symbol"].startswith("AAPL")
        assert kwargs["symbol"].endswith("00150000")  # strike * 1000

    def test_successful_covered_call_caps_contracts(self, monkeypatch):
        """Holding 250 shares + asking for 5 contracts → capped to 2."""
        from options_trader import execute_option_strategy
        self._patch_account_state(monkeypatch,
            [{"symbol": "AAPL", "qty": "250"}],
            {"equity": 100000, "buying_power": 100000})
        api = MagicMock()
        api.submit_order.return_value = MagicMock(id="opt-cc")
        result = execute_option_strategy(api, {
            "option_strategy": "covered_call", "symbol": "AAPL",
            "strike": 200, "expiry": "2099-01-01",
            "contracts": 5,  # asks for 5
        }, ctx=self._ctx(), log=False)
        assert result["action"] == "OPTIONS_OPEN"
        assert result["qty"] == 2  # capped at 250 // 100
        kwargs = api.submit_order.call_args.kwargs
        assert kwargs["side"] == "sell"  # short the call
        assert kwargs["qty"] == 2


# ---------------------------------------------------------------------------
# _validate_ai_trades — OPTIONS pass-through
# ---------------------------------------------------------------------------

class TestValidateOptionsAction:
    def test_options_action_passes_through_with_fields(self):
        """OPTIONS bypass equity gates and carry through option fields."""
        from ai_analyst import _validate_ai_trades
        result = {"trades": [{
            "symbol": "AAPL", "action": "OPTIONS",
            "option_strategy": "covered_call", "strike": 175.0,
            "expiry": "2026-05-16", "contracts": 1,
            "limit_price": 2.50, "confidence": 70,
            "reasoning": "IV high",
        }]}
        candidates = [{"symbol": "AAPL", "indicators": {}}]
        validated = _validate_ai_trades(result, candidates)
        assert len(validated["trades"]) == 1
        v = validated["trades"][0]
        assert v["action"] == "OPTIONS"
        assert v["option_strategy"] == "covered_call"
        assert v["strike"] == 175.0
        assert v["expiry"] == "2026-05-16"
        assert v["contracts"] == 1
        assert v["confidence"] == 70
