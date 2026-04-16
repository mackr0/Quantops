"""Tests for options_oracle.py — Phase 5 of Quant Fund Evolution.

These tests use synthetic options chains (no yfinance calls) to verify
each calculation independently. Network-dependent functions (_fetch_chain,
compute_iv_rank) are not tested here — they're verified in deployment.
"""

import pytest


@pytest.fixture
def synthetic_chain():
    """A well-formed options chain with mixed signals."""
    import pandas as pd

    calls = pd.DataFrame({
        "strike":            [90,   95,   100,  105,  110],
        "impliedVolatility": [0.40, 0.30, 0.25, 0.28, 0.35],
        "volume":            [50,   300,  1000, 500,  100],
        "openInterest":      [500,  2000, 8000, 3000, 1000],
        "bid":               [10.5, 6.0,  2.5,  0.8,  0.2],
        "ask":               [11.0, 6.5,  3.0,  1.0,  0.3],
        "lastPrice":         [10.75, 6.25, 2.75, 0.9,  0.25],
    })
    puts = pd.DataFrame({
        "strike":            [90,   95,   100,  105,  110],
        "impliedVolatility": [0.32, 0.35, 0.38, 0.42, 0.48],
        "volume":            [50,   400,  1200, 400,  100],
        "openInterest":      [500,  3000, 6000, 2500, 800],
        "bid":               [0.2,  0.8,  2.5,  6.0,  10.5],
        "ask":               [0.3,  1.0,  3.0,  6.5,  11.0],
        "lastPrice":         [0.25, 0.9,  2.75, 6.25, 10.75],
    })
    near_chain = {
        "expiration": "2026-05-15",
        "calls": calls,
        "puts": puts,
    }
    far_calls = calls.copy()
    far_calls["impliedVolatility"] = far_calls["impliedVolatility"] * 1.1   # higher IV for farther
    far_chain = {
        "expiration": "2026-08-15",
        "calls": far_calls,
        "puts": puts,
    }

    return {
        "current_price": 100.0,
        "expirations": ["2026-05-15", "2026-08-15"],
        "near_term": near_chain,
        "chains": [near_chain, far_chain],
    }


class TestIVSkew:
    def test_fear_when_puts_more_expensive(self, synthetic_chain):
        from options_oracle import compute_iv_skew
        # put_iv at strike 95 is 0.35, call_iv at 105 is 0.28 → skew 1.25 (neutral)
        # Force extreme skew by boosting put IV
        synthetic_chain["near_term"]["puts"].loc[1, "impliedVolatility"] = 0.60
        result = compute_iv_skew(synthetic_chain)
        assert result["skew"] > 1.3
        assert result["signal"] == "fear"

    def test_greed_when_calls_more_expensive(self, synthetic_chain):
        from options_oracle import compute_iv_skew
        synthetic_chain["near_term"]["calls"].loc[3, "impliedVolatility"] = 0.60
        synthetic_chain["near_term"]["puts"].loc[1, "impliedVolatility"] = 0.15
        result = compute_iv_skew(synthetic_chain)
        assert result["skew"] < 0.85
        assert result["signal"] == "greed"

    def test_handles_missing_iv(self):
        from options_oracle import compute_iv_skew
        import pandas as pd
        calls = pd.DataFrame({"strike": [100], "impliedVolatility": [0]})
        puts = pd.DataFrame({"strike": [100], "impliedVolatility": [0]})
        chain = {"current_price": 100.0, "near_term": {
            "calls": calls, "puts": puts, "expiration": "2026-05-15"}}
        result = compute_iv_skew(chain)
        assert result["signal"] == "neutral"


class TestTermStructure:
    def test_normal_upward_slope(self, synthetic_chain):
        from options_oracle import compute_term_structure
        result = compute_term_structure(synthetic_chain)
        assert result["signal"] == "normal"
        assert result["inverted"] is False

    def test_inverted_signals_event(self, synthetic_chain):
        from options_oracle import compute_term_structure
        # Crank near-term IV far above far-term
        synthetic_chain["chains"][0]["calls"]["impliedVolatility"] = \
            synthetic_chain["chains"][0]["calls"]["impliedVolatility"] * 2.5
        result = compute_term_structure(synthetic_chain)
        assert result["inverted"] is True
        assert result["signal"] == "event_expected"

    def test_single_expiration_returns_normal(self):
        from options_oracle import compute_term_structure
        result = compute_term_structure({"chains": [], "current_price": 100})
        assert result["signal"] == "normal"


class TestImpliedMove:
    def test_computes_reasonable_move(self, synthetic_chain):
        from options_oracle import compute_implied_move
        result = compute_implied_move(synthetic_chain)
        # ATM straddle price ~ 5.5 (call 2.75 + put 2.75), move ~ 5.5 * 0.85 = 4.7%
        assert 2.0 < result["implied_move_pct"] < 15.0

    def test_empty_chain(self):
        from options_oracle import compute_implied_move
        import pandas as pd
        result = compute_implied_move({"current_price": 100, "near_term": {
            "calls": pd.DataFrame(), "puts": pd.DataFrame(),
            "expiration": "2026-05-15"}})
        assert result["implied_move_pct"] == 0.0


class TestPutCallRatios:
    def test_bearish_flow_detected(self, synthetic_chain):
        from options_oracle import compute_put_call_ratios
        # Boost put volume to trigger bearish_flow
        synthetic_chain["near_term"]["puts"]["volume"] = \
            synthetic_chain["near_term"]["puts"]["volume"] * 5
        result = compute_put_call_ratios(synthetic_chain)
        assert result["vol_pcr"] > 1.2
        assert result["signal"] == "bearish_flow"

    def test_bullish_flow_detected(self, synthetic_chain):
        from options_oracle import compute_put_call_ratios
        # Crush put volume
        synthetic_chain["near_term"]["puts"]["volume"] = \
            synthetic_chain["near_term"]["puts"]["volume"] * 0.1
        result = compute_put_call_ratios(synthetic_chain)
        assert result["vol_pcr"] < 0.5
        assert result["signal"] == "bullish_flow"


class TestGammaExposure:
    def test_positive_when_calls_dominate(self, synthetic_chain):
        from options_oracle import compute_gamma_exposure
        # Make calls dominate near-ATM OI
        synthetic_chain["near_term"]["calls"].loc[2, "openInterest"] = 30_000
        result = compute_gamma_exposure(synthetic_chain)
        assert result["gex_sign"] == "positive"

    def test_negative_when_puts_dominate(self, synthetic_chain):
        from options_oracle import compute_gamma_exposure
        synthetic_chain["near_term"]["puts"].loc[2, "openInterest"] = 30_000
        result = compute_gamma_exposure(synthetic_chain)
        assert result["gex_sign"] == "negative"

    def test_neutral_when_balanced(self, synthetic_chain):
        from options_oracle import compute_gamma_exposure
        # Existing chain already balanced
        result = compute_gamma_exposure(synthetic_chain)
        assert result["gex_sign"] == "neutral"


class TestMaxPain:
    def test_finds_minimum_pain_strike(self, synthetic_chain):
        from options_oracle import compute_max_pain
        result = compute_max_pain(synthetic_chain)
        # Max pain should be somewhere near the heaviest OI cluster
        assert 85 <= result["max_pain_strike"] <= 115
        assert "distance_pct" in result


class TestOracleIntegration:
    def test_crypto_symbol_returns_no_options(self):
        from options_oracle import get_options_oracle
        result = get_options_oracle("BTC/USD")
        assert result["has_options"] is False

    def test_summarize_returns_none_when_no_options(self):
        from options_oracle import summarize_for_ai
        assert summarize_for_ai({"has_options": False}) is None

    def test_summarize_returns_string_with_signals(self):
        from options_oracle import summarize_for_ai
        mock_oracle = {
            "has_options": True,
            "skew": {"signal": "fear", "skew": 1.45},
            "term_structure": {"inverted": True},
            "implied_move": {"implied_move_pct": 6.2, "days_to_expiration": 4},
            "pcr": {"signal": "bearish_flow", "vol_pcr": 1.8},
            "gex": {"regime": "volatility_expansion"},
            "max_pain": {"pinning": False, "max_pain_strike": 100},
            "iv_rank": {"signal": "iv_high", "rank_pct": 82},
        }
        summary = summarize_for_ai(mock_oracle)
        assert summary is not None
        assert "fear" in summary
        assert "INVERTED" in summary
        assert "bearish" in summary


class TestPipelineIntegration:
    def test_options_oracle_in_ai_prompt(self):
        from ai_analyst import _build_batch_prompt
        candidates = [{
            "symbol": "AAPL",
            "price": 200.0,
            "signal": "BUY", "score": 2,
            "rsi": 45, "volume_ratio": 1.2, "adx": 28,
            "stoch_rsi": 40, "roc_10": 2.0, "pct_from_52w_high": -5,
            "reason": "test",
            "options_oracle_summary": "skew=fear(1.45) | implied_move=5.2%/4d | PCR=1.80(bearish_flow)",
        }]
        portfolio = {"equity": 10000, "cash": 5000, "positions": [],
                     "num_positions": 0, "drawdown_pct": 0,
                     "drawdown_action": "normal"}
        market = {"regime": "bull", "vix": 18, "spy_trend": "up"}
        prompt = _build_batch_prompt(candidates, portfolio, market)
        assert "OPTIONS" in prompt
        assert "skew=fear" in prompt
