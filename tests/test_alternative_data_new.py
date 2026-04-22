"""Tests for the 8 new alternative data sources (2026-04-22).

Covers: congressional trading, FINRA short volume, insider cluster detection,
analyst estimate revisions (per-symbol), yield curve, ETF flows, CBOE skew,
FRED macro indicators (market-wide).

Also enforces that all new features have display names and meta-model entries.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "quantopsai.db")
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alt_data_cache (
            cache_key TEXT PRIMARY KEY,
            data_json TEXT,
            fetched_at REAL NOT NULL
        )
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr("alternative_data._DB_PATH", db)
    monkeypatch.setattr("alternative_data._table_ensured", False)
    return db


@pytest.fixture
def tmp_macro_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "quantopsai.db")
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alt_data_cache (
            cache_key TEXT PRIMARY KEY,
            data_json TEXT,
            fetched_at REAL NOT NULL
        )
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr("macro_data._DB_PATH", db)
    monkeypatch.setattr("macro_data._table_ensured", False)
    return db


# ---------------------------------------------------------------------------
# Per-symbol: Congressional Trading
# ---------------------------------------------------------------------------

class TestCongressionalTrading:
    def test_returns_default_on_api_failure(self, tmp_db, monkeypatch):
        from alternative_data import get_congressional_trading
        monkeypatch.setattr("alternative_data._http_lock", MagicMock())
        with patch("alternative_data.urlopen", side_effect=Exception("timeout")):
            result = get_congressional_trading("AAPL")
        assert result["net_direction"] == "neutral"
        assert result["recent_transactions"] == 0
        assert isinstance(result["total_value"], (int, float))

    def test_parses_buy_transactions(self, tmp_db, monkeypatch):
        from alternative_data import get_congressional_trading
        fake_data = json.dumps([
            {"TransactionDate": "2026-04-01", "Transaction": "Purchase",
             "Amount": "$1,001 - $15,000", "Representative": "Sen. Smith"},
            {"TransactionDate": "2026-04-05", "Transaction": "Purchase",
             "Amount": "$15,001 - $50,000", "Representative": "Rep. Jones"},
        ]).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = fake_data
        with patch("alternative_data.urlopen", return_value=mock_resp):
            result = get_congressional_trading("AAPL")
        assert result["net_direction"] == "buying"
        assert result["recent_transactions"] == 2

    def test_crypto_skipped(self, tmp_db):
        from alternative_data import get_all_alternative_data
        result = get_all_alternative_data("BTC/USD")
        assert result.get("is_crypto") is True
        assert "congressional" not in result


# ---------------------------------------------------------------------------
# Per-symbol: FINRA Short Volume
# ---------------------------------------------------------------------------

class TestFinraShortVolume:
    def test_returns_default_on_failure(self, tmp_db, monkeypatch):
        from alternative_data import get_finra_short_volume
        with patch("alternative_data.urlopen", side_effect=Exception("404")):
            result = get_finra_short_volume("AAPL")
        assert result["short_volume_ratio"] == 0
        assert result["is_elevated"] is False

    def test_parses_pipe_delimited_data(self, tmp_db, monkeypatch):
        from alternative_data import get_finra_short_volume
        fake_text = (
            "Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market\n"
            "20260422|AAPL|5000000|100000|8000000|Q\n"
            "20260422|MSFT|3000000|50000|7000000|Q\n"
        ).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = fake_text
        with patch("alternative_data.urlopen", return_value=mock_resp):
            result = get_finra_short_volume("AAPL")
        assert result["short_volume"] == 5000000
        assert result["total_volume"] == 8000000
        assert result["short_volume_ratio"] == 0.625
        assert result["is_elevated"] is True


# ---------------------------------------------------------------------------
# Per-symbol: Insider Cluster Detection
# ---------------------------------------------------------------------------

class TestInsiderCluster:
    def test_no_cluster_with_few_insiders(self, tmp_db, monkeypatch):
        from alternative_data import get_insider_cluster
        import pandas as pd
        mock_ticker = MagicMock()
        mock_ticker.insider_transactions = pd.DataFrame()
        with patch.object(
            __import__("yfinance"), "Ticker", return_value=mock_ticker
        ):
            result = get_insider_cluster("AAPL")
        assert result["is_cluster"] is False
        assert result["insider_count"] == 0


# ---------------------------------------------------------------------------
# Per-symbol: Analyst Estimate Revisions
# ---------------------------------------------------------------------------

class TestAnalystEstimates:
    def test_returns_default_on_missing_data(self, tmp_db, monkeypatch):
        from alternative_data import get_analyst_estimates
        mock_ticker = MagicMock()
        mock_ticker.earnings_estimate = None
        mock_ticker.revenue_estimate = None
        with patch.object(
            __import__("yfinance"), "Ticker", return_value=mock_ticker
        ):
            result = get_analyst_estimates("AAPL")
        assert result["eps_revision_direction"] == "flat"
        assert result["eps_current_estimate"] == 0


# ---------------------------------------------------------------------------
# Market-wide: Yield Curve
# ---------------------------------------------------------------------------

class TestYieldCurve:
    def test_returns_default_on_fred_failure(self, tmp_macro_db, monkeypatch):
        from macro_data import get_yield_curve
        with patch("macro_data._fred_fetch", side_effect=Exception("FRED down")):
            result = get_yield_curve()
        assert result["curve_status"] == "normal"
        assert result["rate_10y"] == 0

    def test_detects_inversion(self, tmp_macro_db, monkeypatch):
        from macro_data import get_yield_curve
        def mock_fetch(series_id, limit=5):
            return {"DGS2": [4.50], "DGS10": [4.10], "DGS30": [4.30],
                    "DFEDTARU": [5.50]}.get(series_id, [0])
        monkeypatch.setattr("macro_data._fred_fetch", mock_fetch)
        result = get_yield_curve()
        assert result["curve_status"] == "inverted"
        assert result["spread_10y_2y"] < 0


# ---------------------------------------------------------------------------
# Market-wide: ETF Flows
# ---------------------------------------------------------------------------

class TestETFFlows:
    def test_returns_empty_on_no_bars(self, tmp_macro_db, monkeypatch):
        from macro_data import get_etf_flows
        monkeypatch.setattr("market_data.get_bars", lambda sym, limit=200: None)
        result = get_etf_flows()
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# Market-wide: CBOE Skew
# ---------------------------------------------------------------------------

class TestCBOESkew:
    def test_returns_default_on_failure(self, tmp_macro_db, monkeypatch):
        from macro_data import get_cboe_skew
        import yfinance as yf
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = MagicMock(empty=True)
        with patch.object(yf, "Ticker", return_value=mock_ticker):
            result = get_cboe_skew()
        assert result["skew_signal"] == "normal"
        assert result["skew_value"] == 0


# ---------------------------------------------------------------------------
# Market-wide: FRED Macro
# ---------------------------------------------------------------------------

class TestFREDMacro:
    def test_returns_default_on_failure(self, tmp_macro_db, monkeypatch):
        from macro_data import get_fred_macro
        monkeypatch.setattr("macro_data._fred_fetch", MagicMock(side_effect=Exception("down")))
        result = get_fred_macro()
        assert result["unemployment_rate"] == 0
        assert result["unemployment_trend"] == "stable"

    def test_detects_rising_unemployment(self, tmp_macro_db, monkeypatch):
        from macro_data import get_fred_macro
        def mock_fetch(series_id, limit=5):
            if series_id == "UNRATE":
                return [4.5, 4.3, 4.1]  # latest first, rising
            if series_id == "CPIAUCSL":
                return [310.0] + [0] * 12  # need 13 values
            return [0]
        monkeypatch.setattr("macro_data._fred_fetch", mock_fetch)
        result = get_fred_macro()
        assert result["unemployment_rate"] == 4.5
        assert result["unemployment_trend"] == "rising"


# ---------------------------------------------------------------------------
# Aggregators
# ---------------------------------------------------------------------------

class TestAggregators:
    def test_get_all_alternative_data_includes_new_sources(self, tmp_db, monkeypatch):
        """All 9 sources (5 existing + 4 new) must be in the aggregated dict."""
        from alternative_data import get_all_alternative_data
        # Mock all external calls to return defaults quickly
        monkeypatch.setattr("alternative_data._yf_lock", MagicMock())
        monkeypatch.setattr("alternative_data._http_lock", MagicMock())
        mock_ticker = MagicMock()
        mock_ticker.insider_transactions = None
        mock_ticker.earnings_estimate = None
        mock_ticker.revenue_estimate = None
        mock_ticker.info = {}
        mock_ticker.fast_info = MagicMock(short_ratio=0, shares_short=0)
        mock_ticker.history.return_value = MagicMock(empty=True)
        with patch.object(__import__("yfinance"), "Ticker", return_value=mock_ticker):
            with patch("alternative_data.urlopen", side_effect=Exception("skip")):
                result = get_all_alternative_data("TEST")
        expected_keys = ["insider", "short", "fundamentals", "options", "intraday",
                         "congressional", "finra_short_vol", "insider_cluster",
                         "analyst_estimates"]
        for key in expected_keys:
            assert key in result, f"Missing key '{key}' in get_all_alternative_data"

    def test_get_all_macro_data_has_four_sources(self, tmp_macro_db, monkeypatch):
        from macro_data import get_all_macro_data
        monkeypatch.setattr("macro_data._fred_fetch", MagicMock(return_value=[0]))
        import yfinance as yf
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = MagicMock(empty=True)
        with patch.object(yf, "Ticker", return_value=mock_ticker):
            with patch("market_data.get_bars", return_value=None):
                result = get_all_macro_data()
        for key in ["yield_curve", "etf_flows", "cboe_skew", "fred_macro"]:
            assert key in result, f"Missing key '{key}' in get_all_macro_data"


# ---------------------------------------------------------------------------
# Crisis detector integration
# ---------------------------------------------------------------------------

class TestCrisisDetectorNewSignals:
    def test_skew_extreme_triggers_signal(self, monkeypatch):
        from crisis_detector import _check_cboe_skew
        monkeypatch.setattr("macro_data.get_cboe_skew",
                            lambda: {"skew_value": 155, "skew_signal": "extreme"})
        readings = {}
        signal = _check_cboe_skew(readings)
        assert signal is not None
        assert signal["name"] == "skew_extreme"
        assert readings["cboe_skew"] == 155

    def test_skew_normal_no_signal(self, monkeypatch):
        from crisis_detector import _check_cboe_skew
        monkeypatch.setattr("macro_data.get_cboe_skew",
                            lambda: {"skew_value": 120, "skew_signal": "normal"})
        readings = {}
        signal = _check_cboe_skew(readings)
        assert signal is None

    def test_yield_curve_inversion_triggers_signal(self, monkeypatch):
        from crisis_detector import _check_yield_curve_inversion
        monkeypatch.setattr("macro_data.get_yield_curve",
                            lambda: {"spread_10y_2y": -0.15, "rate_10y": 4.1, "curve_status": "inverted"})
        readings = {}
        signal = _check_yield_curve_inversion(readings)
        assert signal is not None
        assert signal["name"] == "yield_curve_inverted"

    def test_normal_yield_curve_no_signal(self, monkeypatch):
        from crisis_detector import _check_yield_curve_inversion
        monkeypatch.setattr("macro_data.get_yield_curve",
                            lambda: {"spread_10y_2y": 0.50, "rate_10y": 4.5, "curve_status": "normal"})
        readings = {}
        signal = _check_yield_curve_inversion(readings)
        assert signal is None


# ---------------------------------------------------------------------------
# Display names and meta-model feature coverage
# ---------------------------------------------------------------------------

class TestFeatureCoverage:
    def test_all_new_features_have_display_names(self):
        """Every new feature in the meta-model must have a display name."""
        from display_names import _DISPLAY_NAMES
        from meta_model import NUMERIC_FEATURES, CATEGORICAL_FEATURES

        all_features = list(NUMERIC_FEATURES) + list(CATEGORICAL_FEATURES.keys())
        for name in all_features:
            assert name in _DISPLAY_NAMES, (
                f"Feature '{name}' missing from display_names.py _DISPLAY_NAMES"
            )

    def test_new_crisis_signals_have_display_names(self):
        """New crisis signals must have display names."""
        from display_names import _DISPLAY_NAMES
        for name in ["skew_extreme", "yield_curve_inverted",
                      "cboe_skew", "yield_spread_10y2y"]:
            assert name in _DISPLAY_NAMES, (
                f"Crisis signal '{name}' missing from display_names.py"
            )

    def test_all_alternative_data_results_are_json_serializable(self, tmp_db, monkeypatch):
        """All return values must survive json.dumps without errors."""
        from alternative_data import (get_congressional_trading, get_finra_short_volume,
                                       get_insider_cluster, get_analyst_estimates)
        monkeypatch.setattr("alternative_data._yf_lock", MagicMock())
        monkeypatch.setattr("alternative_data._http_lock", MagicMock())
        mock_ticker = MagicMock()
        mock_ticker.insider_transactions = None
        mock_ticker.earnings_estimate = None
        mock_ticker.revenue_estimate = None
        with patch.object(__import__("yfinance"), "Ticker", return_value=mock_ticker):
            with patch("alternative_data.urlopen", side_effect=Exception("skip")):
                for fn in [get_congressional_trading, get_finra_short_volume,
                           get_insider_cluster, get_analyst_estimates]:
                    result = fn("TEST")
                    # Must not raise
                    serialized = json.dumps(result, default=str)
                    assert isinstance(json.loads(serialized), dict)
