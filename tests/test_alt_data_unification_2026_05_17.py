"""Pins the 2026-05-17 alt-data unification:

Before: macro signals (yield curve, FRED, CBOE skew, ETF flows) lived
in a SEPARATE `macro_data` module wired directly into trade_pipeline.
Caused honest confusion ("we don't have FRED" answered when FRED was
implemented — just in a different bucket).

After: ONE canonical entry point — `get_all_alternative_data(symbol)`
returns every alt-data signal including a `macro` key. Macro is
cached at module level so a 30-symbol cycle fetches it once.

These tests pin the contract so a future engineer can't reintroduce
a second bucket without one of these failing.
"""
from __future__ import annotations

import os
import sys
import time
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


@pytest.fixture(autouse=True)
def _reset_macro_cache():
    """Each test starts with a cold cache so they don't share state."""
    import alternative_data
    alternative_data._MACRO_CACHE["ts"] = 0.0
    alternative_data._MACRO_CACHE["data"] = {}
    yield


class TestMacroKeyInUnifiedReturn:
    def test_get_all_alternative_data_includes_macro_key(self):
        """The 'macro' key is now part of the canonical alt-data
        dict. Any reader looking at the alt-data inventory sees
        macro there, eliminating the 'wait do we have FRED?'
        confusion."""
        from alternative_data import get_all_alternative_data
        # Patch every per-symbol fetcher to return {} so the test
        # focuses on the macro key.
        with patch("alternative_data.get_insider_activity", return_value={}), \
             patch("alternative_data.get_short_interest", return_value={}), \
             patch("alternative_data.get_fundamentals", return_value={}), \
             patch("alternative_data.get_options_unusual", return_value={}), \
             patch("alternative_data.get_intraday_patterns", return_value={}), \
             patch("alternative_data.get_finra_short_volume", return_value={}), \
             patch("alternative_data.get_insider_cluster", return_value={}), \
             patch("alternative_data.get_analyst_estimates", return_value={}), \
             patch("alternative_data.get_insider_earnings_signal", return_value={}), \
             patch("alternative_data.get_dark_pool_volume", return_value={}), \
             patch("alternative_data.get_earnings_surprise", return_value={}), \
             patch("alternative_data.get_congressional_recent", return_value={}), \
             patch("alternative_data.get_13f_institutional", return_value={}), \
             patch("alternative_data.get_biotech_milestones", return_value={}), \
             patch("alternative_data.get_stocktwits_sentiment", return_value={}), \
             patch("alternative_data.get_google_trends_signal", return_value={}), \
             patch("alternative_data.get_wikipedia_pageviews_signal", return_value={}), \
             patch("alternative_data.get_app_store_ranking", return_value={}), \
             patch("macro_data.get_all_macro_data", return_value={"yield_curve": "snapshot"}):
            result = get_all_alternative_data("AAPL")
        assert "macro" in result
        assert result["macro"] == {"yield_curve": "snapshot"}

    def test_crypto_symbol_still_includes_macro(self):
        """Crypto skips per-symbol signals (no SEC/options/insider
        data for crypto) but macro IS symbol-agnostic — yield curve
        matters for crypto too. Must still be present."""
        from alternative_data import get_all_alternative_data
        with patch(
            "macro_data.get_all_macro_data",
            return_value={"yield_curve": "snapshot"},
        ):
            result = get_all_alternative_data("BTC/USD")
        assert result.get("is_crypto") is True
        assert result.get("macro") == {"yield_curve": "snapshot"}


class TestMacroCacheBehavior:
    def test_macro_fetched_once_per_ttl_window(self):
        """30 calls to _get_cached_macro inside the TTL window result
        in ONE underlying get_all_macro_data call. This is why the
        unification doesn't waste API calls."""
        from alternative_data import _get_cached_macro
        fake_macro = MagicMock(return_value={"yield_curve": "data"})
        with patch("macro_data.get_all_macro_data", fake_macro):
            for _ in range(30):
                _get_cached_macro()
        assert fake_macro.call_count == 1

    def test_macro_refetched_after_ttl_expires(self):
        """Once the TTL elapses, the next call re-fetches."""
        from alternative_data import _get_cached_macro
        import alternative_data
        fake_macro = MagicMock(return_value={"yield_curve": "data"})
        with patch("macro_data.get_all_macro_data", fake_macro):
            _get_cached_macro()
            # Force-expire the cache by zeroing the timestamp
            alternative_data._MACRO_CACHE["ts"] = 0.0
            _get_cached_macro()
        assert fake_macro.call_count == 2

    def test_macro_fetch_failure_returns_empty_not_crash(self):
        """If macro_data.get_all_macro_data raises, the alt-data
        return must still work — degraded but not broken."""
        from alternative_data import get_all_alternative_data
        with patch(
            "macro_data.get_all_macro_data",
            side_effect=ConnectionError("FRED down"),
        ), patch("alternative_data.get_insider_activity", return_value={}), \
             patch("alternative_data.get_short_interest", return_value={}), \
             patch("alternative_data.get_fundamentals", return_value={}), \
             patch("alternative_data.get_options_unusual", return_value={}), \
             patch("alternative_data.get_intraday_patterns", return_value={}), \
             patch("alternative_data.get_finra_short_volume", return_value={}), \
             patch("alternative_data.get_insider_cluster", return_value={}), \
             patch("alternative_data.get_analyst_estimates", return_value={}), \
             patch("alternative_data.get_insider_earnings_signal", return_value={}), \
             patch("alternative_data.get_dark_pool_volume", return_value={}), \
             patch("alternative_data.get_earnings_surprise", return_value={}), \
             patch("alternative_data.get_congressional_recent", return_value={}), \
             patch("alternative_data.get_13f_institutional", return_value={}), \
             patch("alternative_data.get_biotech_milestones", return_value={}), \
             patch("alternative_data.get_stocktwits_sentiment", return_value={}), \
             patch("alternative_data.get_google_trends_signal", return_value={}), \
             patch("alternative_data.get_wikipedia_pageviews_signal", return_value={}), \
             patch("alternative_data.get_app_store_ranking", return_value={}):
            result = get_all_alternative_data("AAPL")
        assert result["macro"] == {}
        # And the other 18 keys all present
        assert "insider" in result and "options" in result


class TestSingleSourceFromTradePipeline:
    """trade_pipeline reads macro from the SAME alternative_data cache
    so there's one source of truth. A future engineer who adds a new
    macro signal in alternative_data will see it appear in
    trade_pipeline automatically — no second wiring."""

    def test_trade_pipeline_macro_reads_from_alternative_data_cache(self):
        """trade_pipeline imports _get_cached_macro from alternative_data,
        not get_all_macro_data from macro_data."""
        import inspect
        import trade_pipeline
        src = inspect.getsource(trade_pipeline)
        # The new canonical path
        assert "from alternative_data import _get_cached_macro" in src, (
            "trade_pipeline must read macro from the alternative_data "
            "cache so there's one canonical source. If you removed this "
            "import, you reintroduced the two-bucket bifurcation."
        )
