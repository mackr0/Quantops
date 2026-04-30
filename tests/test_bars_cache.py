"""Bar fetch caching — daily bars don't change intraday, so a 5-min
TTL cache eliminates redundant network calls within a scan cycle.

History 2026-04-30: prod scans averaged 4 minutes (max 7.5 min)
because relative_weakness_universe iterates the full universe and
calls get_bars(symbol, limit=257) for each. With 200+ symbols per
universe, that's 200+ uncached network round trips per scan. Cache
hits make subsequent fetches free for 5 minutes.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _make_bars(n=10):
    """Minimal valid OHLCV DataFrame."""
    return pd.DataFrame({
        "open": [100.0] * n,
        "high": [101.0] * n,
        "low": [99.0] * n,
        "close": [100.5] * n,
        "volume": [1_000_000] * n,
    })


def test_get_bars_caches_within_ttl():
    """Two get_bars calls for the same symbol should hit the cache on
    the second call — not invoke the underlying fetch."""
    import market_data as md
    md._bars_cache.clear()
    with patch.object(md, "_get_bars_uncached", return_value=_make_bars()) as mock_fetch:
        md.get_bars("AAPL", limit=200)
        md.get_bars("AAPL", limit=200)
        # Second call must NOT trigger another uncached fetch.
        assert mock_fetch.call_count == 1


def test_get_bars_separate_cache_per_limit():
    """Different limits are different cache keys — caching limit=10
    shouldn't satisfy a limit=200 request (which needs more bars)."""
    import market_data as md
    md._bars_cache.clear()
    with patch.object(md, "_get_bars_uncached", return_value=_make_bars()) as mock_fetch:
        md.get_bars("AAPL", limit=10)
        md.get_bars("AAPL", limit=200)
        assert mock_fetch.call_count == 2


def test_get_bars_separate_cache_per_symbol():
    import market_data as md
    md._bars_cache.clear()
    with patch.object(md, "_get_bars_uncached", return_value=_make_bars()) as mock_fetch:
        md.get_bars("AAPL", limit=200)
        md.get_bars("MSFT", limit=200)
        assert mock_fetch.call_count == 2


def test_get_bars_does_not_cache_empty_or_none():
    """Caching None/empty would poison subsequent requests for
    transient failures. Re-fetch instead."""
    import market_data as md
    md._bars_cache.clear()
    with patch.object(md, "_get_bars_uncached", return_value=None) as mock_fetch:
        md.get_bars("BAD", limit=200)
        md.get_bars("BAD", limit=200)
        # Both calls hit the underlying fetch
        assert mock_fetch.call_count == 2


def test_get_bars_cache_expires_after_ttl():
    """After TTL elapses, the next fetch must hit the underlying API."""
    import market_data as md
    md._bars_cache.clear()
    with patch.object(md, "_get_bars_uncached", return_value=_make_bars()) as mock_fetch:
        md.get_bars("AAPL", limit=200)
        # Manually expire the cache entry by rewinding its timestamp
        key = ("AAPL", 200)
        ts, df = md._bars_cache[key]
        md._bars_cache[key] = (ts - md._BARS_CACHE_TTL - 1, df)
        md.get_bars("AAPL", limit=200)
        assert mock_fetch.call_count == 2


def test_universe_iteration_makes_one_call_per_symbol_not_per_strategy():
    """When two strategies both fetch the same symbol within the cache
    window, only ONE underlying fetch happens. This is the entire
    point of the cache for relative_weakness_universe + everything
    else iterating the same universe."""
    import market_data as md
    md._bars_cache.clear()
    universe = ["AAPL", "MSFT", "GOOG", "TSLA"]
    with patch.object(md, "_get_bars_uncached", return_value=_make_bars()) as mock_fetch:
        # Strategy 1 iterates universe
        for s in universe:
            md.get_bars(s, limit=200)
        # Strategy 2 iterates same universe a moment later
        for s in universe:
            md.get_bars(s, limit=200)
        # Total fetches: 4 (one per unique symbol), not 8
        assert mock_fetch.call_count == 4
