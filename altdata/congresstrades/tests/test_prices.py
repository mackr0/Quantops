"""Tests for prices.py — cache behavior, politeness, and API ergonomics.

Zero real network I/O. The `fetcher` parameter on `refresh_prices` is
a pure callable we inject — tests pass a stub that returns synthetic
DataFrames. This mirrors how pnl.py tests inject price lookups.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from congresstrades.prices import (
    cache_path_for,
    is_cached,
    refresh_prices,
    tickers_from_db,
)
from congresstrades.store import connect, init_db, insert_trade


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_cache(tmp_path):
    return tmp_path / "prices"


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "congress.db"
    monkeypatch.setattr("congresstrades.store.DEFAULT_DB_PATH", str(db))
    init_db(str(db))
    return str(db)


def _make_df(closes):
    """Build a DataFrame shaped like yfinance's single-ticker output."""
    dates = pd.date_range("2025-01-01", periods=len(closes), freq="B")
    return pd.DataFrame({"Close": closes}, index=dates)


# ---------------------------------------------------------------------------
# Caching behavior
# ---------------------------------------------------------------------------

class TestCacheBehavior:
    def test_fetches_when_uncached(self, tmp_cache):
        calls = []
        def fake(ticker, period):
            calls.append((ticker, period))
            return _make_df([100.0, 105.0])
        stats = refresh_prices(
            ["AAPL", "MSFT"], period="1y",
            cache_dir=tmp_cache, delay=0, fetcher=fake,
        )
        assert stats["fetched"] == 2
        assert stats["cached"] == 0
        assert len(calls) == 2

    def test_skips_cached(self, tmp_cache):
        # Pre-warm cache
        tmp_cache.mkdir(parents=True, exist_ok=True)
        (tmp_cache / "AAPL.csv").write_text("Date,Close\n2025-01-01,100\n")

        calls = []
        def fake(ticker, period):
            calls.append(ticker)
            return _make_df([200.0])

        stats = refresh_prices(
            ["AAPL", "MSFT"], cache_dir=tmp_cache, delay=0, fetcher=fake,
        )
        assert stats["cached"] == 1
        assert stats["fetched"] == 1
        assert calls == ["MSFT"]   # AAPL skipped entirely

    def test_force_ignores_cache(self, tmp_cache):
        tmp_cache.mkdir(parents=True, exist_ok=True)
        (tmp_cache / "AAPL.csv").write_text("Date,Close\n2025-01-01,100\n")

        def fake(ticker, period):
            return _make_df([999.0])

        stats = refresh_prices(
            ["AAPL"], cache_dir=tmp_cache, delay=0, fetcher=fake, force=True,
        )
        assert stats["fetched"] == 1
        assert stats["cached"] == 0

    def test_empty_response_still_writes_sentinel_csv(self, tmp_cache):
        """A legitimately-delisted ticker gets an empty CSV so we don't
        re-fetch it every cycle."""
        def fake(ticker, period):
            return pd.DataFrame()  # empty
        stats = refresh_prices(
            ["DEADCO"], cache_dir=tmp_cache, delay=0, fetcher=fake,
        )
        assert stats["empty"] == 1
        assert is_cached("DEADCO", tmp_cache)

    def test_fetch_exception_not_fatal(self, tmp_cache):
        def fake(ticker, period):
            return None   # simulates exception caught upstream
        stats = refresh_prices(
            ["A", "B", "C"], cache_dir=tmp_cache, delay=0, fetcher=fake,
        )
        assert stats["errors"] == 3

    def test_cache_path_is_stable(self, tmp_cache):
        assert cache_path_for("AAPL", tmp_cache).name == "AAPL.csv"


class TestPoliteness:
    def test_default_delay_is_one_second(self):
        """Contract: the default delay value is what we committed to at
        the yfinance-rate-limit investigation. A regression would
        silently make us aggressive to Yahoo."""
        from congresstrades.prices import REQUEST_DELAY_SEC
        assert REQUEST_DELAY_SEC >= 1.0, (
            "Prices cache delay < 1s risks Yahoo rate-limiting — "
            "we validated 1 req/sec with the ~600-ticker bulk fetch."
        )

    def test_delay_zero_for_tests(self, tmp_cache):
        """Tests should be able to bypass the 1s delay."""
        import time
        def fake(ticker, period):
            return _make_df([1.0])
        t0 = time.time()
        refresh_prices(["A", "B", "C"], cache_dir=tmp_cache,
                       delay=0, fetcher=fake)
        elapsed = time.time() - t0
        # 3 fetches shouldn't take anywhere near 3 seconds when delay=0
        assert elapsed < 0.5


class TestProgressCallback:
    def test_progress_called_per_ticker(self, tmp_cache):
        events = []
        def on_progress(n, total):
            events.append((n, total))
        def fake(ticker, period):
            return _make_df([1.0])
        refresh_prices(
            ["A", "B", "C", "D"],
            cache_dir=tmp_cache, delay=0, fetcher=fake,
            on_progress=on_progress,
        )
        assert events == [(1, 4), (2, 4), (3, 4), (4, 4)]


# ---------------------------------------------------------------------------
# DB-driven ticker discovery
# ---------------------------------------------------------------------------

class TestTickersFromDb:
    def _add(self, conn, **kw):
        base = {
            "chamber": "house", "member_name": "X",
            "filing_doc_id": f"doc-{kw.get('ticker')}-{kw.get('transaction_date')}",
            "asset_description": kw.get("ticker", "X"),
            "amount_range": "A",
        }
        base.update(kw)
        insert_trade(conn, base)

    def test_empty_db(self, tmp_db):
        with connect(tmp_db) as conn:
            assert tickers_from_db(conn) == []

    def test_distinct_tickers(self, tmp_db):
        with connect(tmp_db) as conn:
            self._add(conn, ticker="AAPL", transaction_date="2025-01-01")
            self._add(conn, ticker="AAPL", transaction_date="2025-02-01")
            self._add(conn, ticker="MSFT", transaction_date="2025-03-01")
            self._add(conn, ticker=None, transaction_date="2025-04-01")
            assert tickers_from_db(conn) == ["AAPL", "MSFT"]

    def test_filter_by_year(self, tmp_db):
        with connect(tmp_db) as conn:
            self._add(conn, ticker="OLD", transaction_date="2023-05-01")
            self._add(conn, ticker="NEW", transaction_date="2025-05-01")
            assert tickers_from_db(conn, year=2025) == ["NEW"]

    def test_filter_by_chamber(self, tmp_db):
        with connect(tmp_db) as conn:
            self._add(conn, chamber="house",  ticker="HSE",
                      transaction_date="2025-01-01")
            self._add(conn, chamber="senate", ticker="SEN",
                      transaction_date="2025-01-01")
            assert tickers_from_db(conn, chamber="house") == ["HSE"]
            assert tickers_from_db(conn, chamber="senate") == ["SEN"]

    def test_null_tickers_excluded(self, tmp_db):
        with connect(tmp_db) as conn:
            self._add(conn, ticker=None, transaction_date="2025-01-01")
            assert tickers_from_db(conn) == []
