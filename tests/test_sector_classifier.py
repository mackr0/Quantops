"""Guardrails for `sector_classifier.py` — DYNAMIC_UNIVERSE_PLAN.md
Step 1.

The module replaces the hand-rolled 50-symbol `_SECTOR_MAP` in
`market_data._guess_sector` with a SQLite-cached yfinance lookup +
fallback map + default. These tests prove:

1. Cache hit returns cached value, doesn't call yfinance.
2. Cache miss writes a row when yfinance succeeds.
3. yfinance failure falls back to the static map.
4. Static-map miss falls back to "tech" default.
5. Stale cache entries are bypassed and re-fetched.
6. `market_data._guess_sector` is now a thin wrapper around the new
   helper (source-level contract).
"""

from __future__ import annotations

import inspect
import os
import sqlite3
import tempfile
from unittest.mock import patch

import pytest


@pytest.fixture
def fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    # Reset the module-level schema-init cache so the test DB gets
    # a fresh schema build.
    import sector_classifier as sc
    sc._schema_initialized.clear()
    yield path
    try:
        os.unlink(path)
    except Exception:
        pass


def test_internal_sectors_are_seven():
    """The 7-key taxonomy is part of the contract — strategies and
    relative-strength code depend on it."""
    import sector_classifier as sc
    assert len(sc.INTERNAL_SECTORS) == 7


def test_cache_hit_returns_cached_value(fresh_db):
    """Pre-populate the cache with AAPL=tech. A subsequent call must
    not invoke yfinance."""
    import sector_classifier as sc
    sc._init_schema(fresh_db)
    conn = sqlite3.connect(fresh_db)
    conn.execute(
        "INSERT OR REPLACE INTO sector_cache "
        "(symbol, sector, fetched_at) VALUES (?, ?, datetime('now'))",
        ("AAPL", "tech"),
    )
    conn.commit()
    conn.close()

    with patch("sector_classifier._yfinance_sector") as mock_yf:
        result = sc.get_sector("AAPL", db_path=fresh_db)
    assert result == "tech"
    assert mock_yf.call_count == 0, (
        "Cache hit must NOT call yfinance — defeats the cache's purpose."
    )


def test_cache_miss_writes_row_after_yfinance(fresh_db):
    import sector_classifier as sc

    with patch("sector_classifier._yfinance_sector", return_value="finance"):
        result = sc.get_sector("JPM", db_path=fresh_db)
    assert result == "finance"

    # Verify the cache row was written
    conn = sqlite3.connect(fresh_db)
    row = conn.execute(
        "SELECT sector FROM sector_cache WHERE symbol = ?", ("JPM",),
    ).fetchone()
    conn.close()
    assert row is not None and row[0] == "finance"


def test_yfinance_failure_falls_back_to_static_map(fresh_db):
    """When yfinance returns None, the static fallback map should be
    consulted. AAPL is in the fallback map under 'tech'."""
    import sector_classifier as sc

    with patch("sector_classifier._yfinance_sector", return_value=None):
        result = sc.get_sector("AAPL", db_path=fresh_db)
    assert result == "tech"


def test_unknown_symbol_returns_tech_default(fresh_db):
    """A symbol that's not in yfinance and not in the fallback map
    returns 'tech' (matches the prior behavior of `_guess_sector`)."""
    import sector_classifier as sc

    with patch("sector_classifier._yfinance_sector", return_value=None):
        result = sc.get_sector("ZZZUNKNOWNZZZ", db_path=fresh_db)
    assert result == "tech"


def test_stale_cache_is_bypassed_and_refreshed(fresh_db):
    """Insert a row with `fetched_at` > 7 days ago. Next call should
    treat it as stale and re-fetch via yfinance."""
    import sector_classifier as sc
    from datetime import datetime, timedelta
    sc._init_schema(fresh_db)
    stale_ts = (datetime.utcnow() - timedelta(days=10)).isoformat()
    conn = sqlite3.connect(fresh_db)
    conn.execute(
        "INSERT OR REPLACE INTO sector_cache "
        "(symbol, sector, fetched_at) VALUES (?, ?, ?)",
        ("MSFT", "energy", stale_ts),  # deliberately wrong sector
    )
    conn.commit()
    conn.close()

    with patch("sector_classifier._yfinance_sector",
               return_value="tech") as mock_yf:
        result = sc.get_sector("MSFT", db_path=fresh_db)

    assert mock_yf.call_count == 1, (
        "Stale cache must be bypassed and yfinance re-queried."
    )
    assert result == "tech"


def test_market_data_guess_sector_uses_classifier_module():
    """Source-level contract: market_data._guess_sector must delegate
    to sector_classifier.get_sector. The old hardcoded _SECTOR_MAP
    must be gone."""
    import market_data
    src = inspect.getsource(market_data._guess_sector)
    assert "sector_classifier" in src or "get_sector" in src, (
        "REGRESSION: market_data._guess_sector no longer delegates to "
        "sector_classifier. The hardcoded _SECTOR_MAP was removed in "
        "DYNAMIC_UNIVERSE_PLAN.md Step 1; reverting it would mean "
        "future renames/sector reclassifications never update."
    )
    # Also assert the old hardcoded dict is GONE
    assert "_SECTOR_MAP" not in src, (
        "_SECTOR_MAP hardcoded dict must not return — that's the very "
        "thing the new module replaces."
    )
