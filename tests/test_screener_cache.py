"""Regression tests for the dynamic-screener cache + budget (2026-04-15)."""

from __future__ import annotations

import json
import os
import time
from unittest.mock import patch, MagicMock

import pytest


class TestDiskCachePersistence:
    def test_save_and_load_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("screener._DYNAMIC_CACHE_FILE",
                            "dynamic_screener_cache.json")
        import screener
        # Seed in-memory cache and save
        screener._dynamic_cache = {
            "small_1.0_20.0_500000": (1_700_000_000.0, ["AAPL", "MSFT", "NVDA"])
        }
        screener._save_disk_cache()
        assert os.path.exists("dynamic_screener_cache.json")

        # Wipe in-memory, reload
        screener._dynamic_cache = {}
        screener._load_disk_cache()
        assert "small_1.0_20.0_500000" in screener._dynamic_cache
        ts, syms = screener._dynamic_cache["small_1.0_20.0_500000"]
        assert ts == 1_700_000_000.0
        assert syms == ["AAPL", "MSFT", "NVDA"]

    def test_missing_file_is_graceful(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("screener._DYNAMIC_CACHE_FILE", "nonexistent.json")
        import screener
        screener._dynamic_cache = {}
        screener._load_disk_cache()   # must not raise
        assert screener._dynamic_cache == {}


class TestStaleFallback:
    """When yfinance fails, prefer a stale cache over the hardcoded fallback."""

    def test_exception_path_returns_stale_cache(self, monkeypatch):
        import screener

        # Seed a stale cache entry (simulates a previous successful run)
        cache_key = "largecap_50.0_500.0_1000000"
        stale_symbols = ["AAPL", "MSFT", "GOOG"]
        screener._dynamic_cache = {
            cache_key: (time.time() - 7200, stale_symbols),  # 2 hrs old
        }

        # Force yfinance to fail
        monkeypatch.setattr(
            "screener.yf.download",
            MagicMock(side_effect=RuntimeError("yfinance hammered")),
        )

        # Mock the Alpaca assets list so we reach the download step
        class FakeAsset:
            def __init__(self, symbol, exchange="NYSE"):
                self.tradable = True
                self.exchange = exchange
                self.symbol = symbol
        fake_assets = [FakeAsset(f"SYM{i}") for i in range(200)]

        class FakeApi:
            def list_assets(self, status="active"):
                return fake_assets
        monkeypatch.setattr("client.get_api", lambda ctx=None: FakeApi())

        # Cache entry is older than 0 but younger than TTL, so the
        # fresh-cache check skips. Then download fails. Then we should
        # return the stale cache.
        monkeypatch.setattr("screener._DYNAMIC_TTL", 3600)  # 1h — stale is 2h

        result = screener.screen_dynamic_universe(
            min_price=50.0, max_price=500.0, min_volume=1_000_000,
            market_type="largecap", fallback_universe=["SPY", "QQQ"],
        )
        assert result == stale_symbols


class TestBudgetConstants:
    """The yfinance budget must be bounded to prevent 30-min hangs."""

    def test_budget_is_reasonable(self):
        import screener
        assert 30 <= screener._DYNAMIC_YF_BUDGET_SEC <= 600, (
            f"yfinance budget should be 30s-10min; "
            f"got {screener._DYNAMIC_YF_BUDGET_SEC}s"
        )
