"""Tests for the yfinance → Alpaca market-data migration (2026-04-15).

Covers:
  - get_bars returns the same DataFrame shape from Alpaca as from yfinance
    (lowercase OHLCV columns, US/Eastern tz-aware index)
  - Alpaca is tried first; yfinance is the fallback when Alpaca returns
    None/empty or raises
  - Crypto symbols bypass Alpaca and go straight to yfinance
  - Screener uses Alpaca snapshots first; falls back to yfinance on failure
  - _limit_to_days produces sensible windows
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# Helpers — build fake Alpaca / yfinance responses with the right shape
# ---------------------------------------------------------------------------

def _alpaca_bars_df(days=30, start_price=100.0):
    """Build a DataFrame shaped like what `client.get_bars(...).df` returns."""
    idx = pd.date_range("2026-01-01", periods=days, freq="B", tz="UTC")
    price = [start_price + i * 0.5 for i in range(days)]
    return pd.DataFrame({
        "open":        [p - 0.1 for p in price],
        "high":        [p + 0.3 for p in price],
        "low":         [p - 0.3 for p in price],
        "close":       price,
        "volume":      [1_000_000 + i * 1000 for i in range(days)],
        "trade_count": [5000] * days,
        "vwap":        price,
    }, index=idx)


def _yf_history_df(days=30, start_price=50.0):
    """Build a DataFrame shaped like what `yf.Ticker(...).history()` returns.

    yfinance columns are title-case (Open/High/Low/Close/Volume) before
    our code lowercases them.
    """
    idx = pd.date_range("2026-01-01", periods=days, freq="B", tz="US/Eastern")
    price = [start_price + i * 0.2 for i in range(days)]
    return pd.DataFrame({
        "Open":   [p - 0.1 for p in price],
        "High":   [p + 0.2 for p in price],
        "Low":    [p - 0.2 for p in price],
        "Close":  price,
        "Volume": [500_000 + i * 500 for i in range(days)],
    }, index=idx)


# ---------------------------------------------------------------------------
# _limit_to_days — calendar-day lookback mapping
# ---------------------------------------------------------------------------

class TestLimitToDays:
    def test_short_windows_include_weekend_buffer(self):
        from market_data import _limit_to_days
        # 5 trading days → ~10 calendar days (covers 1 weekend)
        assert _limit_to_days(5) >= 7
        # 22 trading days (1 month) → ~35 calendar days
        assert _limit_to_days(22) >= 30

    def test_long_windows_scale_with_limit(self):
        from market_data import _limit_to_days
        d252 = _limit_to_days(252)
        d504 = _limit_to_days(504)
        assert d252 >= 365       # >= 1 year
        assert d504 >= 700       # >= 2 years
        assert d504 > d252        # monotonic

    def test_huge_limit_has_cap(self):
        from market_data import _limit_to_days
        d = _limit_to_days(5000)
        assert 1500 <= d <= 2000    # ~5 years


# ---------------------------------------------------------------------------
# get_bars — Alpaca is the primary path
# ---------------------------------------------------------------------------

class TestGetBarsAlpacaPrimary:
    def test_alpaca_success_returns_lowercase_ohlcv(self, monkeypatch):
        """Alpaca returns raw data with lowercase columns + trade_count/vwap.
        get_bars should strip to just the 5 OHLCV columns we use."""
        from market_data import get_bars

        fake_client = MagicMock()
        fake_bars_response = SimpleNamespace(df=_alpaca_bars_df(days=30))
        fake_client.get_bars.return_value = fake_bars_response
        monkeypatch.setattr("market_data._get_alpaca_data_client",
                            lambda: fake_client)

        df = get_bars("AAPL", limit=30)
        assert not df.empty
        # Lowercase OHLCV columns only, no trade_count / vwap leakage
        assert set(df.columns) == {"open", "high", "low", "close", "volume"}
        # US/Eastern tz (not UTC from Alpaca)
        assert str(df.index.tz).startswith("US/Eastern") or \
               str(df.index.tz) == "America/New_York"

    def test_alpaca_tail_respects_limit(self, monkeypatch):
        """Alpaca over-fetches calendar days; result must be truncated to `limit`."""
        from market_data import get_bars

        fake_client = MagicMock()
        # Return 100 bars, but caller asks for 30
        fake_client.get_bars.return_value = SimpleNamespace(df=_alpaca_bars_df(days=100))
        monkeypatch.setattr("market_data._get_alpaca_data_client",
                            lambda: fake_client)

        df = get_bars("AAPL", limit=30)
        assert len(df) == 30

    def test_alpaca_empty_falls_back_to_yfinance(self, monkeypatch):
        """Alpaca returned nothing — yfinance should be the fallback."""
        from market_data import get_bars

        fake_client = MagicMock()
        fake_client.get_bars.return_value = SimpleNamespace(df=pd.DataFrame())
        monkeypatch.setattr("market_data._get_alpaca_data_client",
                            lambda: fake_client)

        fake_ticker = MagicMock()
        fake_ticker.history.return_value = _yf_history_df(days=30)
        monkeypatch.setattr("market_data.yf.Ticker", lambda s: fake_ticker)

        df = get_bars("OBSCURE_SYMBOL", limit=30)
        assert not df.empty
        # yfinance title-case columns should have been lowercased by the fallback
        assert set(df.columns) == {"open", "high", "low", "close", "volume"}
        # yfinance started at $50; Alpaca fake started at $100 — confirm we
        # actually got the yfinance data, not a stale Alpaca response
        assert df["close"].iloc[0] < 100

    def test_alpaca_exception_falls_back_to_yfinance(self, monkeypatch):
        """Alpaca raised (rate limit / 500 / etc) — yfinance fallback."""
        from market_data import get_bars

        fake_client = MagicMock()
        fake_client.get_bars.side_effect = RuntimeError("Alpaca 500")
        monkeypatch.setattr("market_data._get_alpaca_data_client",
                            lambda: fake_client)

        fake_ticker = MagicMock()
        fake_ticker.history.return_value = _yf_history_df(days=30)
        monkeypatch.setattr("market_data.yf.Ticker", lambda s: fake_ticker)

        df = get_bars("AAPL", limit=30)
        assert not df.empty

    def test_no_alpaca_client_falls_back(self, monkeypatch):
        """When Alpaca creds aren't configured at all (e.g. in tests),
        yfinance handles it."""
        from market_data import get_bars

        monkeypatch.setattr("market_data._get_alpaca_data_client",
                            lambda: None)
        fake_ticker = MagicMock()
        fake_ticker.history.return_value = _yf_history_df(days=10)
        monkeypatch.setattr("market_data.yf.Ticker", lambda s: fake_ticker)

        df = get_bars("AAPL", limit=10)
        assert not df.empty


# ---------------------------------------------------------------------------
# Crypto symbols bypass Alpaca
# ---------------------------------------------------------------------------

class TestCryptoBypassesAlpaca:
    def test_slash_symbol_goes_straight_to_yfinance(self, monkeypatch):
        """BTC/USD and similar crypto symbols must skip the Alpaca equity
        endpoint (which doesn't serve them) and go straight to yfinance.
        The yf symbol should have the slash converted to a dash."""
        from market_data import get_bars

        fake_alpaca = MagicMock()
        fake_alpaca.get_bars.side_effect = AssertionError(
            "Alpaca should not be called for crypto symbols"
        )
        monkeypatch.setattr("market_data._get_alpaca_data_client",
                            lambda: fake_alpaca)

        captured_symbol = {"sym": None}

        class FakeTicker:
            def __init__(self, sym):
                captured_symbol["sym"] = sym
            def history(self, **kw):
                return _yf_history_df(days=5)

        monkeypatch.setattr("market_data.yf.Ticker", FakeTicker)

        df = get_bars("BTC/USD", limit=5)
        assert not df.empty
        # Slash → dash conversion for yfinance
        assert captured_symbol["sym"] == "BTC-USD"


# ---------------------------------------------------------------------------
# Screener — Alpaca snapshots primary, yfinance fallback
# ---------------------------------------------------------------------------

def _fake_snapshot(close, vol):
    """Build a mock that matches the Alpaca Snapshot object shape we read."""
    daily = SimpleNamespace(c=close, v=vol)
    return SimpleNamespace(daily_bar=daily)


class TestScreenerUsesAlpaca:
    def test_screener_alpaca_path_returns_filtered_symbols(self, monkeypatch,
                                                            tmp_path):
        """Screener should call get_snapshots, filter by price+volume,
        and return matches."""
        monkeypatch.chdir(tmp_path)
        # Reset module-level cache for a clean test
        import screener
        screener._dynamic_cache = {}

        # Fake Alpaca assets list (step 1)
        class FakeAsset:
            def __init__(self, sym, ex="NYSE"):
                self.tradable = True
                self.exchange = ex
                self.symbol = sym
        fake_assets = [FakeAsset(f"SYM{i}") for i in range(200)]

        class FakeApi:
            def list_assets(self, status="active"):
                return fake_assets
        monkeypatch.setattr("client.get_api", lambda ctx=None: FakeApi())

        # Fake snapshots (step 2) — only first 5 symbols pass the filter
        fake_snaps = {}
        for i in range(200):
            close = 100.0 if i < 5 else 300.0   # only i<5 match min_price 50, max 200
            vol   = 2_000_000 if i < 5 else 100_000
            fake_snaps[f"SYM{i}"] = _fake_snapshot(close=close, vol=vol)

        fake_client = MagicMock()
        fake_client.get_snapshots.return_value = fake_snaps
        monkeypatch.setattr("market_data._get_alpaca_data_client",
                            lambda: fake_client)

        # yfinance must NOT be called on the happy path
        monkeypatch.setattr(
            "screener.yf.download",
            MagicMock(side_effect=AssertionError("yfinance should not be called")),
        )

        # Prevent the fallback_universe from adding extra symbols that bump
        # the sample size unpredictably.
        result = screener.screen_dynamic_universe(
            min_price=50.0, max_price=200.0, min_volume=1_000_000,
            market_type="test_market", fallback_universe=None,
        )
        # All 5 qualifying symbols should appear
        assert len(result) == 5
        assert all(s.startswith("SYM") for s in result)

    def test_screener_alpaca_failure_falls_back_to_yfinance(self, monkeypatch,
                                                            tmp_path):
        """If Alpaca snapshots raises, yfinance bulk download is tried."""
        monkeypatch.chdir(tmp_path)
        import screener
        screener._dynamic_cache = {}

        class FakeAsset:
            def __init__(self, sym):
                self.tradable = True
                self.exchange = "NYSE"
                self.symbol = sym
        fake_assets = [FakeAsset(f"SYM{i}") for i in range(120)]

        class FakeApi:
            def list_assets(self, status="active"):
                return fake_assets
        monkeypatch.setattr("client.get_api", lambda ctx=None: FakeApi())

        # Alpaca path fails
        fake_client = MagicMock()
        fake_client.get_snapshots.side_effect = RuntimeError("Alpaca down")
        monkeypatch.setattr("market_data._get_alpaca_data_client",
                            lambda: fake_client)

        # yfinance fallback returns a valid (small) multi-index DataFrame
        idx = pd.date_range("2026-01-01", periods=5, freq="B", tz="US/Eastern")
        # Build the MultiIndex shape yf.download returns for multi-symbol
        cols = pd.MultiIndex.from_product([["Close", "Volume"], ["SYM0", "SYM1"]])
        data = [[100, 100, 2_000_000, 2_000_000] for _ in range(5)]
        yf_df = pd.DataFrame(data, index=idx, columns=cols)

        monkeypatch.setattr("screener.yf.download",
                            lambda *a, **kw: yf_df)

        result = screener.screen_dynamic_universe(
            min_price=50.0, max_price=200.0, min_volume=1_000_000,
            market_type="test_fallback", fallback_universe=["SYM0", "SYM1"],
        )
        # yfinance returned SYM0+SYM1 with qualifying price+vol
        assert len(result) >= 1


# ---------------------------------------------------------------------------
# Contract tests — source pattern guards against regression
# ---------------------------------------------------------------------------

class TestMigrationContract:
    def test_market_data_equity_path_tries_alpaca_before_yfinance(self):
        """Guard against someone accidentally reordering the fallback.

        The whole point of the migration is Alpaca first, yfinance second,
        for equity symbols. Crypto is allowed to skip Alpaca directly.
        If the equity ordering flips back, we're one market-open yfinance
        hang away from the 30-minute incident."""
        import inspect, market_data
        src = inspect.getsource(market_data.get_bars)
        # The crypto (slash) branch returns yfinance directly — skip past
        # that chunk and inspect the equity path only.
        slash_branch_end = src.find('return _fetch_via_yfinance(symbol, limit)')
        assert slash_branch_end > 0, "crypto-bypass path missing"
        # After the slash branch, Alpaca must come before the final yfinance fallback
        equity_src = src[slash_branch_end + 50:]
        alpaca_idx = equity_src.find("_fetch_via_alpaca")
        yfinance_idx = equity_src.find("_fetch_via_yfinance")
        assert alpaca_idx > 0, "Alpaca call missing from equity path"
        assert yfinance_idx > 0, "yfinance fallback missing from equity path"
        assert alpaca_idx < yfinance_idx, (
            "market_data.get_bars equity path must try Alpaca BEFORE yfinance. "
            "Reversing this order re-introduces the 30-minute screener hang."
        )

    def test_screener_tries_alpaca_before_yfinance(self):
        """Same invariant for the screener."""
        import inspect, screener
        src = inspect.getsource(screener.screen_dynamic_universe)
        alpaca_idx = src.find("get_snapshots")
        yfinance_idx = src.find("yf_lock.download")
        assert alpaca_idx > 0
        assert yfinance_idx > 0
        assert alpaca_idx < yfinance_idx, (
            "screen_dynamic_universe must call Alpaca get_snapshots BEFORE "
            "yfinance yf.download"
        )

    def test_screener_equity_functions_use_alpaca(self):
        """screen_by_price_range, find_volume_surges, find_momentum_stocks,
        find_breakouts must use market_data.get_bars (Alpaca), not yf.download."""
        import inspect, screener
        for fn_name in ("screen_by_price_range", "find_volume_surges",
                        "find_momentum_stocks", "find_breakouts"):
            fn = getattr(screener, fn_name)
            src = inspect.getsource(fn)
            assert "yf_lock.download" not in src and "yf.download" not in src, (
                f"screener.{fn_name} still uses yfinance batch download. "
                f"Must use _get_bars_for_symbols (Alpaca) instead."
            )
            assert "_get_bars_for_symbols" in src or "get_bars" in src, (
                f"screener.{fn_name} must use Alpaca via get_bars or _get_bars_for_symbols"
            )

    def test_no_yfinance_in_equity_price_paths(self):
        """Critical files that fetch equity prices must use Alpaca, not yfinance.
        yfinance is only acceptable for: crypto, VIX, fundamentals, insider data,
        options chains, earnings dates, analyst recs, news."""
        import inspect

        # ai_tracker: must use api.get_latest_trade, not yf
        import ai_tracker
        src = inspect.getsource(ai_tracker._get_current_price)
        assert "get_latest_trade" in src, (
            "ai_tracker._get_current_price must use api.get_latest_trade as primary"
        )
        assert "yf.Ticker" not in src and "yf.download" not in src, (
            "ai_tracker._get_current_price must not use yfinance directly"
        )

        # correlation: must use market_data.get_bars
        import correlation
        src = inspect.getsource(correlation._fetch_returns)
        assert "yf_lock.download" not in src and "yf.download" not in src, (
            "correlation._fetch_returns must use Alpaca via get_bars, not yfinance"
        )

        # metrics: must use market_data.get_bars_daterange
        import metrics
        src = inspect.getsource(metrics._fetch_benchmark_returns)
        assert "yf_lock.download" not in src and "yf.download" not in src, (
            "metrics._fetch_benchmark_returns must use Alpaca, not yfinance"
        )

        # market_data: get_snapshot must try Alpaca first
        import market_data
        src = inspect.getsource(market_data.get_snapshot)
        assert "get_latest_trade" in src or "_get_alpaca_data_client" in src, (
            "market_data.get_snapshot must try Alpaca before yfinance"
        )

        # market_data: get_bars_daterange must try Alpaca first
        src = inspect.getsource(market_data.get_bars_daterange)
        assert "_get_alpaca_data_client" in src, (
            "market_data.get_bars_daterange must try Alpaca before yfinance"
        )

        # market_data: get_sector_rotation must use get_bars
        src = inspect.getsource(market_data.get_sector_rotation)
        assert "yf_lock.download" not in src and "yf.download" not in src, (
            "market_data.get_sector_rotation must use Alpaca via get_bars"
        )

        # market_data: get_relative_strength_vs_sector must use get_bars
        src = inspect.getsource(market_data.get_relative_strength_vs_sector)
        assert "yf.Ticker" not in src, (
            "market_data.get_relative_strength_vs_sector must use get_bars, not yf.Ticker"
        )

    def test_multi_scheduler_loads_dotenv(self):
        """multi_scheduler.py must call load_dotenv() before importing modules
        that use env vars (market_data, client, etc). Without this, the Alpaca
        data client gets empty keys and silently falls back to yfinance."""
        import inspect, multi_scheduler
        src = inspect.getsource(multi_scheduler)
        dotenv_idx = src.find("load_dotenv()")
        assert dotenv_idx > 0, "multi_scheduler must call load_dotenv()"
        # Check it comes before the actual import statement (not docstring mentions)
        import_idx = src.find("\nfrom segments import")
        assert import_idx > 0, "multi_scheduler must import from segments"
        assert dotenv_idx < import_idx, (
            "multi_scheduler must call load_dotenv() BEFORE importing "
            "other modules that read env vars"
        )

    def test_backtester_uses_alpaca(self):
        """Backtester must use market_data.get_bars, not yf.download."""
        import inspect, backtester
        src_download = inspect.getsource(backtester._download_symbol)
        assert "yf.Ticker" not in src_download and "yf.download" not in src_download, (
            "backtester._download_symbol must use Alpaca via get_bars_daterange"
        )
        src_batch = inspect.getsource(backtester._fetch_universe_batch)
        assert "yf.download" not in src_batch, (
            "backtester._fetch_universe_batch must use Alpaca via get_bars"
        )

    def test_app_loads_dotenv(self):
        """app.py (gunicorn entry point) must call load_dotenv() so the web
        process has Alpaca credentials for dashboard data fetches (sector
        rotation, snapshots, etc)."""
        import inspect, app
        src = inspect.getsource(app)
        assert "load_dotenv()" in src, (
            "app.py must call load_dotenv() — without it, the gunicorn web "
            "process has no env vars and all Alpaca data calls fail silently"
        )
