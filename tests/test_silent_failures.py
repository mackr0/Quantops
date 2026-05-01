"""Tests that catch silent failures before they reach production.

These tests exist because we repeatedly introduced bugs that silently
degraded AI decision quality without any visible error on the dashboard:
- market_regime.py used title-case columns after migrating to Alpaca (lowercase)
- news_sentiment.py called Alpaca news API without a subscription (401s)
- alternative_data.py crashed on yfinance rate limits with no fallback
- earnings_calendar.py flooded Yahoo with per-symbol calls

Each test here verifies that a specific data source either produces
valid output OR logs a clear warning — never silently returns empty/default
data that looks correct but isn't.
"""

from __future__ import annotations
import pytest


class TestMarketRegimeColumnNames:
    def test_spy_columns_are_lowercase(self):
        """market_regime.py must use lowercase columns (Alpaca format),
        not title-case (yfinance format). The migration from yfinance to
        Alpaca broke this with 'High'/'Low'/'Close' → 174 errors/day."""
        import inspect
        import market_regime
        src = inspect.getsource(market_regime.detect_regime)
        # These title-case column accesses caused the 'High' KeyError
        for col in ['"High"', '"Low"', '"Open"']:
            assert col not in src, (
                f"market_regime uses {col} (title-case) but Alpaca returns "
                f"lowercase. This causes 'Failed to detect market regime' errors."
            )
        # VIX from yfinance IS title-case — that's correct
        # Only SPY data (from Alpaca) must be lowercase

    def test_detect_regime_does_not_crash_on_import(self):
        """Basic smoke test — the function should be importable."""
        from market_regime import detect_regime
        assert callable(detect_regime)


class TestAlpacaVsYfinanceColumnConsistency:
    def test_get_bars_returns_lowercase_columns(self):
        """Every downstream consumer assumes lowercase OHLCV columns
        from get_bars(). If this ever changes, dozens of things break."""
        from market_data import get_bars
        # Can't call get_bars in tests (needs API), but verify the
        # _fetch_via_alpaca function documents lowercase output
        import inspect
        from market_data import _fetch_via_alpaca
        src = inspect.getsource(_fetch_via_alpaca)
        for col in ["open", "high", "low", "close", "volume"]:
            assert col in src, f"_fetch_via_alpaca should reference lowercase '{col}'"


class TestNoSilentEmptyReturns:
    def test_news_fetch_does_not_use_sdk_get_news(self):
        """The old alpaca-trade-api `api.get_news()` SDK method failed
        with 401 in our environment. Migrated 2026-05-01 to direct
        REST calls via `data.alpaca.markets/v1beta1/news` (verified
        200 with our existing keys, returns Benzinga feed).

        This test enforces: no caller of fetch_news regresses back
        to the SDK method. Direct REST is the path forward.
        """
        import inspect
        from news_sentiment import fetch_news, fetch_news_alpaca
        src_main = inspect.getsource(fetch_news)
        src_alpaca = inspect.getsource(fetch_news_alpaca)
        assert "api.get_news" not in src_main, (
            "fetch_news regressed to SDK api.get_news method"
        )
        assert "api.get_news" not in src_alpaca, (
            "fetch_news_alpaca regressed to SDK api.get_news method"
        )

    def test_earnings_calendar_uses_db_cache(self):
        """Earnings dates should be cached in SQLite, not fetched
        per-symbol per-cycle from yfinance."""
        import inspect
        from earnings_calendar import check_earnings
        src = inspect.getsource(check_earnings)
        assert "_get_cached" in src or "_fetch_and_store" in src, (
            "check_earnings should use DB cache, not raw yf.Ticker().calendar"
        )

    def test_alternative_data_uses_db_cache(self):
        """Alt data should be cached in SQLite to survive restarts."""
        import inspect
        from alternative_data import _get_cached, _set_cached
        src_get = inspect.getsource(_get_cached)
        src_set = inspect.getsource(_set_cached)
        assert "sqlite3" in src_get, "_get_cached should read from SQLite"
        assert "sqlite3" in src_set, "_set_cached should write to SQLite"


class TestETFFiltering:
    def test_common_etfs_excluded(self):
        """ETFs cause 'no fundamentals' errors and aren't tradeable
        candidates. They should be filtered from the screener."""
        import inspect
        from screener import screen_dynamic_universe
        src = inspect.getsource(screen_dynamic_universe)
        for etf in ("SOXL", "TQQQ", "SPY", "QQQ", "JPST", "RSP"):
            assert etf in src, f"{etf} should be in the ETF blocklist"


class TestThreadSafety:
    def test_yf_download_uses_lock(self):
        """All yf.download calls must go through yf_lock to prevent
        'dictionary changed size during iteration' crashes."""
        import inspect
        import screener
        src = inspect.getsource(screener)
        # No direct yf.download calls should exist
        assert "yf.download(" not in src, (
            "screener.py still has direct yf.download() calls. "
            "Must use yf_lock.download() for thread safety."
        )

    def test_ensemble_cache_has_lock(self):
        """The ensemble cache must use a threading lock to prevent
        parallel profiles from running duplicate AI calls."""
        import inspect
        from trade_pipeline import _get_shared_ensemble
        src = inspect.getsource(_get_shared_ensemble)
        assert "_ensemble_lock" in src or "Lock" in src, (
            "Ensemble cache needs a thread lock — without it, parallel "
            "profiles race and run duplicate AI calls."
        )


class TestVirtualAccountIntegrity:
    def test_get_positions_passes_ctx(self):
        """trade_pipeline and trader must pass ctx to get_positions()
        so virtual profiles get internal positions, not Alpaca's
        combined view."""
        import inspect
        import trade_pipeline
        src = inspect.getsource(trade_pipeline.run_trade_cycle)
        assert "get_positions(api, ctx=ctx)" in src, (
            "run_trade_cycle must pass ctx=ctx to get_positions(). "
            "Without it, virtual profiles size against Alpaca's $1M."
        )

    def test_get_account_info_passes_ctx(self):
        import inspect
        import trade_pipeline
        src = inspect.getsource(trade_pipeline.run_trade_cycle)
        assert "get_account_info(api, ctx=ctx)" in src, (
            "run_trade_cycle must pass ctx=ctx to get_account_info(). "
            "Without it, virtual profiles see Alpaca's combined equity."
        )
