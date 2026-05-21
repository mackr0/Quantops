"""Unit tests for alt_data_cache module (docs/21, 2026-05-20).

Pins:
  1. cache_get / cache_set round-trip with TTL respected
  2. Expired entries are not returned (lazy TTL check on read)
  3. cache_or_fetch calls fetcher exactly once on cache miss,
     zero times on cache hit
  4. cache_or_fetch never raises — cache failures fall through to live
  5. SOURCE_TTL_SECONDS has sane defaults (no near-zero or negative TTLs)
  6. evict_stale removes only expired rows
  7. cache_stats produces the expected shape
  8. The kill switch (is_enabled / ALTDATA_CACHE_ENABLED env var) works
"""
from __future__ import annotations

import os
import sys
import time
from unittest.mock import MagicMock

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


@pytest.fixture
def cache_in_tmp(tmp_path, monkeypatch):
    """Redirect the cache file to a temp directory so each test
    starts with a clean slate."""
    import alt_data_cache
    cache_dir = tmp_path / "altdata" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_db = str(cache_dir / "static_altdata.db")
    monkeypatch.setattr(alt_data_cache, "_CACHE_DIR", str(cache_dir))
    monkeypatch.setattr(alt_data_cache, "_CACHE_DB", cache_db)
    return alt_data_cache


# ---------------------------------------------------------------------------
# (1) Round-trip
# ---------------------------------------------------------------------------

def test_cache_set_then_get_round_trip(cache_in_tmp):
    c = cache_in_tmp
    assert c.cache_set("AAPL", "insider", {"net": 5}, ttl_seconds=60)
    got = c.cache_get("AAPL", "insider")
    assert got == {"net": 5}


def test_cache_get_returns_none_for_missing_entry(cache_in_tmp):
    assert cache_in_tmp.cache_get("AAPL", "insider") is None


def test_cache_get_is_case_normalized(cache_in_tmp):
    """cache_set upper-cases symbol; cache_get must match the same
    normalization or all calls miss."""
    c = cache_in_tmp
    c.cache_set("aapl", "insider", {"x": 1}, ttl_seconds=60)
    assert c.cache_get("AAPL", "insider") == {"x": 1}
    assert c.cache_get("aapl", "insider") == {"x": 1}


def test_cache_set_upsert_replaces_existing(cache_in_tmp):
    """Re-writing (symbol, source) replaces the prior row — only one
    row per pair exists at any time."""
    c = cache_in_tmp
    c.cache_set("AAPL", "insider", {"v": 1}, ttl_seconds=60)
    c.cache_set("AAPL", "insider", {"v": 2}, ttl_seconds=60)
    assert c.cache_get("AAPL", "insider") == {"v": 2}


# ---------------------------------------------------------------------------
# (2) TTL
# ---------------------------------------------------------------------------

def test_expired_entry_returns_none(cache_in_tmp, monkeypatch):
    """Set TTL=0 → entry expires instantly → cache_get returns None."""
    c = cache_in_tmp
    c.cache_set("AAPL", "insider", {"v": 1}, ttl_seconds=0)
    # Sleep a tick to be sure the now-time has advanced past expires_at
    time.sleep(0.05)
    assert c.cache_get("AAPL", "insider") is None


# ---------------------------------------------------------------------------
# (3) cache_or_fetch
# ---------------------------------------------------------------------------

def test_cache_or_fetch_calls_fetcher_on_miss(cache_in_tmp):
    # Post-#186 Phase B: cache_or_fetch annotates returned dicts with
    # `_cached` / `_cached_age_min`. Assert on the payload field
    # directly rather than dict-equality so the test isn't brittle
    # to the freshness annotations.
    fetcher = MagicMock(return_value={"v": 42})
    out = cache_in_tmp.cache_or_fetch(
        "insider", "AAPL", fetcher, ttl_seconds=60,
    )
    assert out["v"] == 42
    assert out.get("_cached") is False
    fetcher.assert_called_once_with("AAPL")


def test_cache_or_fetch_does_not_call_fetcher_on_hit(cache_in_tmp):
    fetcher = MagicMock(return_value={"v": 1})
    # First call: cache miss → fetcher called
    cache_in_tmp.cache_or_fetch("insider", "AAPL", fetcher, ttl_seconds=60)
    fetcher.reset_mock()
    # Second call within TTL: should hit cache, no fetcher call.
    # Post-#186 Phase B annotation: returned dict is tagged _cached=True.
    out = cache_in_tmp.cache_or_fetch("insider", "AAPL", fetcher, ttl_seconds=60)
    assert out["v"] == 1
    assert out.get("_cached") is True
    fetcher.assert_not_called()


def test_cache_or_fetch_writes_to_cache_after_live_fetch(cache_in_tmp):
    """After a cache_or_fetch with cache miss, the next cache_get
    finds the result — proving the fetched value was persisted."""
    c = cache_in_tmp
    fetcher = MagicMock(return_value={"v": 99})
    c.cache_or_fetch("insider", "AAPL", fetcher, ttl_seconds=60)
    assert c.cache_get("AAPL", "insider") == {"v": 99}


def test_cache_or_fetch_skips_cache_write_when_fetcher_returns_none(cache_in_tmp):
    """If the fetcher returns None (e.g., the symbol has no data),
    we don't persist a None row — that would block future re-fetch
    attempts. Caller still gets None back."""
    c = cache_in_tmp
    fetcher = MagicMock(return_value=None)
    out = c.cache_or_fetch("insider", "AAPL", fetcher, ttl_seconds=60)
    assert out is None
    # And the cache should NOT have a row for it
    assert c.cache_get("AAPL", "insider") is None


# ---------------------------------------------------------------------------
# (4) Cache failures don't break the world
# ---------------------------------------------------------------------------

def test_cache_get_returns_none_on_db_error(monkeypatch, tmp_path):
    """If _CACHE_DB points at a directory that can't be created (e.g.,
    permission denied — but here we simulate via patching), cache_get
    returns None and the caller falls through to live fetch."""
    import alt_data_cache
    monkeypatch.setattr(alt_data_cache, "_CACHE_DIR", "/proc/0/nonexistent")
    monkeypatch.setattr(alt_data_cache, "_CACHE_DB", "/proc/0/nonexistent/x.db")
    # MUST NOT raise
    assert alt_data_cache.cache_get("AAPL", "insider") is None


def test_cache_or_fetch_falls_through_to_live_when_cache_breaks(monkeypatch):
    """If the cache layer fails, cache_or_fetch still returns whatever
    the fetcher produced — never blocks on cache error.

    Post-#186 Phase B (2026-05-20): the returned dict is also annotated
    with `_cached: False, _cached_age_min: 0` so downstream consumers
    can tell this came from a live fetch even when the cache write
    silently failed (which is the case here — broken _CACHE_DIR).
    Asserting on the live field directly rather than dict-equality
    to allow the freshness annotations through."""
    import alt_data_cache
    monkeypatch.setattr(alt_data_cache, "_CACHE_DIR", "/proc/0/nonexistent")
    monkeypatch.setattr(alt_data_cache, "_CACHE_DB", "/proc/0/nonexistent/x.db")
    fetcher = MagicMock(return_value={"v": "live"})
    out = alt_data_cache.cache_or_fetch(
        "insider", "AAPL", fetcher, ttl_seconds=60,
    )
    assert out["v"] == "live"
    # Annotation should reflect live-fetch state even on cache fallthrough
    assert out.get("_cached") is False
    fetcher.assert_called_once()


# ---------------------------------------------------------------------------
# (5) SOURCE_TTL_SECONDS sanity
# ---------------------------------------------------------------------------

def test_source_ttl_seconds_all_positive():
    """Every TTL must be > 0; a 0-or-negative TTL would mean the entry
    is born-stale and never serves a hit."""
    from alt_data_cache import SOURCE_TTL_SECONDS
    for source, ttl in SOURCE_TTL_SECONDS.items():
        assert ttl > 0, f"{source}: TTL is non-positive ({ttl})"


def test_source_ttl_seconds_includes_key_sources():
    """Spot-check that the expected daily-cadence sources are
    present. If someone deletes a source from the TTL config, the
    cache_set call for it returns False and the wrapper falls through
    to live fetch — slow cycle, but correct."""
    from alt_data_cache import SOURCE_TTL_SECONDS
    expected_subset = {
        "insider", "institutional_13f", "fundamentals",
        "google_trends", "stocktwits_sentiment",
        "biotech_milestones", "options",
    }
    missing = expected_subset - set(SOURCE_TTL_SECONDS)
    assert not missing, (
        f"Source-TTL config missing key entries: {missing}. "
        f"Pre-warm task will skip them; cycle cold-start tax returns."
    )


def test_source_ttl_minimum_makes_sense():
    """The shortest TTL is for options at 5 min. Anything below 1 min
    would mean re-fetching faster than a typical cycle's duration —
    no useful cache."""
    from alt_data_cache import SOURCE_TTL_SECONDS
    shortest = min(SOURCE_TTL_SECONDS.values())
    assert shortest >= 60, (
        f"Shortest TTL ({shortest}s) is sub-minute — cache won't pay off"
    )


# ---------------------------------------------------------------------------
# (6) Eviction
# ---------------------------------------------------------------------------

def test_evict_stale_removes_expired_rows(cache_in_tmp):
    c = cache_in_tmp
    c.cache_set("AAPL", "insider", {"v": 1}, ttl_seconds=0)
    c.cache_set("MSFT", "insider", {"v": 2}, ttl_seconds=3600)
    time.sleep(0.05)
    removed = c.evict_stale()
    assert removed == 1
    # The fresh entry survived
    assert c.cache_get("MSFT", "insider") == {"v": 2}


def test_evict_stale_returns_zero_when_no_stale_rows(cache_in_tmp):
    c = cache_in_tmp
    c.cache_set("AAPL", "insider", {"v": 1}, ttl_seconds=3600)
    assert c.evict_stale() == 0


# ---------------------------------------------------------------------------
# (7) Stats
# ---------------------------------------------------------------------------

def test_cache_stats_shape(cache_in_tmp):
    c = cache_in_tmp
    c.cache_set("AAPL", "insider", {"v": 1}, ttl_seconds=3600)
    c.cache_set("MSFT", "insider", {"v": 2}, ttl_seconds=3600)
    c.cache_set("AAPL", "fundamentals", {"v": 3}, ttl_seconds=3600)
    stats = c.cache_stats()
    assert stats["total_rows"] == 3
    assert stats["fresh_rows"] == 3
    assert stats["stale_rows"] == 0
    assert "insider" in stats["per_source"]
    assert stats["per_source"]["insider"]["fresh"] == 2
    assert stats["per_source"]["fundamentals"]["fresh"] == 1


# ---------------------------------------------------------------------------
# (8) Kill switch
# ---------------------------------------------------------------------------

def test_is_enabled_default_is_on(monkeypatch):
    monkeypatch.delenv("ALTDATA_CACHE_ENABLED", raising=False)
    from alt_data_cache import is_enabled
    assert is_enabled() is True


def test_is_enabled_off_via_env(monkeypatch):
    monkeypatch.setenv("ALTDATA_CACHE_ENABLED", "0")
    from alt_data_cache import is_enabled
    assert is_enabled() is False


def test_is_enabled_on_for_anything_other_than_zero(monkeypatch):
    """Any value other than literal "0" leaves it on — defensive."""
    from alt_data_cache import is_enabled
    monkeypatch.setenv("ALTDATA_CACHE_ENABLED", "yes")
    assert is_enabled() is True
    monkeypatch.setenv("ALTDATA_CACHE_ENABLED", "1")
    assert is_enabled() is True
    monkeypatch.setenv("ALTDATA_CACHE_ENABLED", "")
    assert is_enabled() is True
