"""Tests for the Apr 17 data fixes: alt data caching, market regime,
ETF filtering, metrics capital calculation.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest


# ---------------------------------------------------------------------------
# Alternative data DB cache
# ---------------------------------------------------------------------------

class TestAltDataCache:
    def test_cache_persists_to_sqlite(self, monkeypatch, tmp_path):
        """Alt data should be cached in SQLite, not just in-memory."""
        monkeypatch.chdir(tmp_path)
        import alternative_data as ad
        db_path = str(tmp_path / "test.db")
        monkeypatch.setattr(ad, "_DB_PATH", db_path)
        monkeypatch.setattr(ad, "_table_ensured", False)

        ad._set_cached("insider_AAPL", {"recent_buys": 5})
        result = ad._get_cached("insider_AAPL", "insider")
        assert result is not None
        assert result["recent_buys"] == 5

    def test_cache_respects_ttl(self, monkeypatch, tmp_path):
        """Expired cache should return None."""
        monkeypatch.chdir(tmp_path)
        import alternative_data as ad
        db_path = str(tmp_path / "test.db")
        monkeypatch.setattr(ad, "_DB_PATH", db_path)
        monkeypatch.setattr(ad, "_table_ensured", False)

        ad._ensure_cache_table()
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO alt_data_cache (cache_key, data_json, fetched_at) "
            "VALUES (?, ?, ?)",
            ("insider_OLD", '{"buys": 1}', 0),
        )
        conn.commit()
        conn.close()

        result = ad._get_cached("insider_OLD", "insider")
        assert result is None

    def test_cache_survives_module_reload(self, monkeypatch, tmp_path):
        """DB cache should survive what a memory cache wouldn't."""
        monkeypatch.chdir(tmp_path)
        import alternative_data as ad
        db_path = str(tmp_path / "test.db")
        monkeypatch.setattr(ad, "_DB_PATH", db_path)
        monkeypatch.setattr(ad, "_table_ensured", False)

        ad._set_cached("fund_MSFT", {"pe": 30.5})
        monkeypatch.setattr(ad, "_table_ensured", False)

        result = ad._get_cached("fund_MSFT", "fundamentals")
        assert result is not None
        assert result["pe"] == 30.5


# ---------------------------------------------------------------------------
# ETF filtering in screener
# ---------------------------------------------------------------------------

class TestETFFilter:
    def test_known_etfs_in_blocklist(self):
        """SOXL, TQQQ, SPY etc. should be excluded from screener."""
        # Can't easily test the full screener (needs Alpaca API),
        # but verify the blocklist is populated and contains key ETFs
        from screener import screen_dynamic_universe
        import inspect
        src = inspect.getsource(screen_dynamic_universe)
        for etf in ("SOXL", "TQQQ", "SQQQ", "SPY", "QQQ", "AMZD", "NVDL"):
            assert etf in src, f"{etf} should be in the ETF blocklist"


# ---------------------------------------------------------------------------
# Market regime uses Alpaca for SPY
# ---------------------------------------------------------------------------

class TestMarketRegimeAlpaca:
    def test_detect_regime_uses_get_bars_not_yf_ticker(self):
        """Market regime should use Alpaca's get_bars for SPY,
        not yf.Ticker which rate-limits."""
        import inspect
        import market_regime
        src = inspect.getsource(market_regime.detect_regime)
        assert "get_bars" in src, "detect_regime should use Alpaca get_bars for SPY"
        # SPY should NOT be fetched via yf.Ticker
        assert 'yf.Ticker("SPY")' not in src


# ---------------------------------------------------------------------------
# Metrics initial_capital
# ---------------------------------------------------------------------------

class TestMetricsCapital:
    def test_capital_by_db_used_for_forward_fill(self):
        """When capital_by_db is provided, each DB should get its own
        initial capital for snapshot forward-fill, not an average."""
        from metrics import _gather_snapshots

        # Create two DBs with different capital levels
        import tempfile
        tmpdir = tempfile.mkdtemp()

        for name, equity in [("small.db", 25000), ("large.db", 1000000)]:
            path = os.path.join(tmpdir, name)
            conn = sqlite3.connect(path)
            conn.execute("""
                CREATE TABLE daily_snapshots (
                    date TEXT, equity REAL, cash REAL,
                    portfolio_value REAL, num_positions INTEGER,
                    daily_pnl REAL
                )
            """)
            conn.execute(
                "INSERT INTO daily_snapshots VALUES (?, ?, ?, ?, ?, ?)",
                ("2026-04-17", equity, equity, 0, 0, 0),
            )
            conn.commit()
            conn.close()

        small_path = os.path.join(tmpdir, "small.db")
        large_path = os.path.join(tmpdir, "large.db")
        db_paths = {small_path, large_path}

        capital_map = {
            small_path: 25000,
            large_path: 1000000,
        }

        snapshots = _gather_snapshots(db_paths, initial_capital_per_profile=capital_map)
        assert len(snapshots) == 1
        # Combined equity should be 25000 + 1000000 = 1025000
        assert snapshots[0]["equity"] == 1025000

    def test_combined_initial_capital_not_multiplied(self):
        """calculate_all_metrics should not multiply total capital by
        num_profiles — the caller already sums per-profile capitals."""
        from metrics import calculate_all_metrics

        m = calculate_all_metrics(
            set(),
            initial_capital=2150000,
            capital_by_db={},
        )
        # With no data, return should be 0, not some large number
        assert m["total_return_pct"] == 0.0


# ---------------------------------------------------------------------------
# Annualized return overflow guard
# ---------------------------------------------------------------------------

class TestAnnualizedReturnOverflow:
    def test_no_overflow_on_short_period(self):
        """With <7 days, annualized return should be 0, not overflow."""
        from metrics import calculate_all_metrics

        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, "short.db")
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE trades (
                timestamp TEXT, symbol TEXT, side TEXT, qty REAL,
                price REAL, pnl REAL, strategy TEXT, decision_price REAL,
                fill_price REAL, slippage_pct REAL, status TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE daily_snapshots (
                date TEXT, equity REAL, cash REAL,
                portfolio_value REAL, num_positions INTEGER, daily_pnl REAL
            )
        """)
        conn.execute(
            "INSERT INTO daily_snapshots VALUES (?, ?, ?, ?, ?, ?)",
            ("2026-04-17", 10500, 10500, 0, 0, 500),
        )
        conn.commit()
        conn.close()

        m = calculate_all_metrics(
            {path}, initial_capital=10000,
            capital_by_db={path: 10000},
        )
        # Should not crash and should return 0 for annualized (< 7 days)
        assert m["annualized_return_pct"] == 0.0
