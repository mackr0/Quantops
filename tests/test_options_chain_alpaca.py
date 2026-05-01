"""Tests for options_chain_alpaca — replaces yfinance for options data.

Coverage:
  - Black-Scholes IV inversion: round-trip from a known IV → price → IV
  - DataFrame builder: Alpaca's per-contract snapshots → yfinance-shape
    DataFrames with the columns downstream code expects
  - fetch_chain_alpaca: integration with mocked HTTP, returns the same
    output shape options_oracle._fetch_chain used to return
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class TestImpliedVolFromPrice:
    def test_round_trip_atm_call(self):
        """Compute price from a known IV, then invert it back."""
        from options_chain_alpaca import _implied_vol_from_price
        from options_trader import compute_greeks
        # ATM call, 30d, IV=0.30
        true_iv = 0.30
        g = compute_greeks(spot=100, strike=100, days_to_expiry=30,
                            iv=true_iv, is_call=True)
        recovered = _implied_vol_from_price(
            market_price=g["price"], spot=100, strike=100,
            days_to_expiry=30, is_call=True,
        )
        assert recovered is not None
        assert abs(recovered - true_iv) < 0.01

    def test_round_trip_otm_put(self):
        from options_chain_alpaca import _implied_vol_from_price
        from options_trader import compute_greeks
        true_iv = 0.45
        g = compute_greeks(spot=150, strike=140, days_to_expiry=45,
                            iv=true_iv, is_call=False)
        recovered = _implied_vol_from_price(
            market_price=g["price"], spot=150, strike=140,
            days_to_expiry=45, is_call=False,
        )
        assert recovered is not None
        assert abs(recovered - true_iv) < 0.01

    def test_below_intrinsic_returns_none(self):
        """ITM call with market price below intrinsic = data is wrong;
        return None rather than fitting a degenerate IV."""
        from options_chain_alpaca import _implied_vol_from_price
        # Call strike 100, spot 110 → intrinsic 10. Market price 5
        # (below intrinsic) is impossible.
        result = _implied_vol_from_price(
            market_price=5.0, spot=110, strike=100,
            days_to_expiry=30, is_call=True,
        )
        assert result is None

    def test_zero_inputs_returns_none(self):
        from options_chain_alpaca import _implied_vol_from_price
        assert _implied_vol_from_price(0, 100, 100, 30, True) is None
        assert _implied_vol_from_price(5, 0, 100, 30, True) is None
        assert _implied_vol_from_price(5, 100, 0, 30, True) is None
        assert _implied_vol_from_price(5, 100, 100, 0, True) is None

    def test_extreme_high_vol_recovered(self):
        """Pre-earnings vol can spike to 100%+. Solver must handle."""
        from options_chain_alpaca import _implied_vol_from_price
        from options_trader import compute_greeks
        true_iv = 1.20  # 120% vol
        g = compute_greeks(spot=100, strike=100, days_to_expiry=14,
                            iv=true_iv, is_call=True)
        recovered = _implied_vol_from_price(
            market_price=g["price"], spot=100, strike=100,
            days_to_expiry=14, is_call=True,
        )
        assert recovered is not None
        assert abs(recovered - true_iv) < 0.05


class TestBuildChainDataframes:
    def _contract(self, symbol, exp, strike, type_="call",
                    open_interest=100):
        return {
            "symbol": symbol, "expiration_date": exp,
            "type": type_, "strike": strike,
            "open_interest": open_interest, "close_price": 0,
        }

    def _snapshot(self, bid, ask, last=None, volume=10):
        return {
            "latestQuote": {"bp": bid, "ap": ask, "bs": 5, "as": 5,
                            "t": "2026-05-01T20:00:00Z"},
            "latestTrade": {"p": last if last is not None else (bid+ask)/2,
                            "s": 1, "t": "2026-05-01T19:00:00Z"},
            "dailyBar": {"o": last or 0, "h": last or 0, "l": last or 0,
                         "c": last or 0, "v": volume,
                         "t": "2026-05-01T04:00:00Z"},
        }

    def test_groups_by_expiration_with_calls_and_puts(self):
        from options_chain_alpaca import _build_chain_dataframes
        future = (date.today() + timedelta(days=30)).isoformat()
        contracts = [
            self._contract("AAPL  990515C00150000", future, 150, "call"),
            self._contract("AAPL  990515P00150000", future, 150, "put"),
            self._contract("AAPL  990515C00155000", future, 155, "call"),
        ]
        snaps = {
            "AAPL  990515C00150000": self._snapshot(2.0, 2.2),
            "AAPL  990515P00150000": self._snapshot(2.5, 2.7),
            "AAPL  990515C00155000": self._snapshot(0.8, 1.0),
        }
        result = _build_chain_dataframes(
            "AAPL", contracts, snaps, spot=150,
        )
        assert future in result
        bucket = result[future]
        assert len(bucket["calls"]) == 2
        assert len(bucket["puts"]) == 1
        assert "impliedVolatility" in bucket["calls"].columns
        assert "strike" in bucket["calls"].columns
        # Strikes sorted ascending
        assert list(bucket["calls"]["strike"]) == [150, 155]

    def test_iv_computed_per_contract(self):
        """Each row's impliedVolatility should be a positive number
        derived from the mid price."""
        from options_chain_alpaca import _build_chain_dataframes
        future = (date.today() + timedelta(days=30)).isoformat()
        contracts = [
            self._contract("AAPL  990515C00150000", future, 150, "call"),
        ]
        snaps = {
            "AAPL  990515C00150000": self._snapshot(2.0, 2.2),
        }
        result = _build_chain_dataframes(
            "AAPL", contracts, snaps, spot=150,
        )
        iv = result[future]["calls"].iloc[0]["impliedVolatility"]
        assert 0.05 < iv < 1.0  # somewhere in the reasonable equity-IV range

    def test_skips_expired_contracts(self):
        from options_chain_alpaca import _build_chain_dataframes
        past = (date.today() - timedelta(days=5)).isoformat()
        contracts = [
            self._contract("AAPL  990515C00150000", past, 150, "call"),
        ]
        snaps = {"AAPL  990515C00150000": self._snapshot(2.0, 2.2)}
        result = _build_chain_dataframes(
            "AAPL", contracts, snaps, spot=150,
        )
        assert past not in result

    def test_missing_snapshot_uses_zero_quotes(self):
        """When Alpaca has the contract but no snapshot, the row still
        appears with zero bid/ask/last and IV=0 (rather than crashing)."""
        from options_chain_alpaca import _build_chain_dataframes
        future = (date.today() + timedelta(days=30)).isoformat()
        contracts = [
            self._contract("AAPL  990515C00150000", future, 150, "call"),
        ]
        result = _build_chain_dataframes(
            "AAPL", contracts, snapshots={}, spot=150,
        )
        # Contract still appears
        bucket = result.get(future, {"calls": []})
        assert len(bucket["calls"]) == 1
        row = bucket["calls"].iloc[0]
        assert row["bid"] == 0
        assert row["ask"] == 0
        # IV would be None → coerced to 0
        assert row["impliedVolatility"] == 0


class TestFetchChainAlpaca:
    def test_crypto_skipped(self):
        from options_chain_alpaca import fetch_chain_alpaca
        # No HTTP calls needed — crypto ('/' in symbol) bypasses
        result = fetch_chain_alpaca("BTC/USD")
        assert result is None

    def test_no_spot_returns_none(self):
        from options_chain_alpaca import fetch_chain_alpaca
        with patch("options_chain_alpaca._underlying_spot",
                   return_value=None):
            result = fetch_chain_alpaca("AAPL")
        assert result is None

    def test_no_contracts_returns_none(self):
        from options_chain_alpaca import fetch_chain_alpaca
        with patch("options_chain_alpaca._underlying_spot",
                   return_value=150.0), \
             patch("options_chain_alpaca._fetch_contracts",
                   return_value=[]):
            result = fetch_chain_alpaca("AAPL")
        assert result is None

    def test_assembles_full_chain_shape(self):
        from options_chain_alpaca import fetch_chain_alpaca
        future = (date.today() + timedelta(days=30)).isoformat()
        contracts = [
            {"symbol": "AAPL  990515C00150000",
             "expiration_date": future, "type": "call",
             "strike": 150.0, "open_interest": 100, "close_price": 2.0},
            {"symbol": "AAPL  990515P00150000",
             "expiration_date": future, "type": "put",
             "strike": 150.0, "open_interest": 80, "close_price": 2.5},
        ]
        snaps = {
            "AAPL  990515C00150000": {
                "latestQuote": {"bp": 2.0, "ap": 2.2,
                                "t": "2026-05-01T20:00:00Z"},
                "latestTrade": {"p": 2.1, "t": "2026-05-01T19:00:00Z"},
                "dailyBar": {"c": 2.1, "v": 100,
                             "t": "2026-05-01T04:00:00Z"},
            },
            "AAPL  990515P00150000": {
                "latestQuote": {"bp": 2.5, "ap": 2.7,
                                "t": "2026-05-01T20:00:00Z"},
                "latestTrade": {"p": 2.6, "t": "2026-05-01T19:00:00Z"},
                "dailyBar": {"c": 2.6, "v": 80,
                             "t": "2026-05-01T04:00:00Z"},
            },
        }
        with patch("options_chain_alpaca._underlying_spot",
                   return_value=150.0), \
             patch("options_chain_alpaca._fetch_contracts",
                   return_value=contracts), \
             patch("options_chain_alpaca._fetch_snapshots",
                   return_value=snaps):
            result = fetch_chain_alpaca("AAPL")

        assert result is not None
        assert result["current_price"] == 150.0
        assert result["expirations"] == [future]
        assert "near_term" in result
        assert "chains" in result
        # near_term has calls + puts DataFrames
        nt = result["near_term"]
        assert nt["expiration"] == future
        assert len(nt["calls"]) == 1
        assert len(nt["puts"]) == 1
