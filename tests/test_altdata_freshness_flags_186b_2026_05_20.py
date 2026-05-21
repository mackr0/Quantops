"""#186 Phase B — alt-data freshness flags (2026-05-20).

`alt_data_cache.cache_or_fetch` now annotates dict-shaped payloads
with `_cached` (bool) and `_cached_age_min` (int) so downstream
consumers can tell live fetches from cache hits and reason about
data age.

`ai_analyst._build_batch_prompt` renders a single freshness summary
line per candidate's alt-data block: "[Freshness: X live, Y cached
(oldest Nh:MMm)]". Single line keeps the prompt compact while giving
the AI explicit signal about which data is fresh.

Tests pin:
  1. cache_or_fetch with live fetch annotates `_cached: False`,
     `_cached_age_min: 0`
  2. cache_or_fetch with cache hit annotates `_cached: True` plus
     a non-negative age
  3. The annotations don't persist into the next cache row (added
     AFTER cache_set, not before)
  4. Non-dict return values pass through unannotated (no AttributeError)
  5. AI prompt freshness summary appears when at least one source
     has the annotation; omitted when no sources do
  6. Freshness summary counts live vs cached correctly
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from types import SimpleNamespace

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


# ---------------------------------------------------------------------------
# 1-4. cache_or_fetch annotation behavior
# ---------------------------------------------------------------------------

@pytest.fixture
def cache_in_tmp(tmp_path, monkeypatch):
    """Redirect the cache DB into a temp dir so tests don't share state."""
    import alt_data_cache
    cache_dir = tmp_path / "altdata" / "cache"
    cache_dir.mkdir(parents=True)
    cache_db = str(cache_dir / "static_altdata.db")
    monkeypatch.setattr(alt_data_cache, "_CACHE_DIR", str(cache_dir))
    monkeypatch.setattr(alt_data_cache, "_CACHE_DB", cache_db)
    return alt_data_cache


class TestCacheOrFetchAnnotations:
    def test_live_fetch_annotates_cached_false(self, cache_in_tmp):
        from alt_data_cache import cache_or_fetch
        fetched = cache_or_fetch(
            "insider", "AAPL", lambda s: {"score": 7, "sym": s},
        )
        assert fetched["_cached"] is False, (
            "Live fetch must annotate _cached: False so downstream "
            "can tell this is fresh data."
        )
        assert fetched["_cached_age_min"] == 0

    def test_cache_hit_annotates_cached_true(self, cache_in_tmp):
        from alt_data_cache import cache_or_fetch
        # First call: live fetch + cache write
        cache_or_fetch("insider", "AAPL", lambda s: {"score": 7})
        # Second call: cache hit
        second = cache_or_fetch(
            "insider", "AAPL", lambda s: {"score": 99},  # should not be called
        )
        assert second["_cached"] is True
        assert second["score"] == 7, "Cache hit returned live fetcher's value"
        age = second.get("_cached_age_min")
        assert age is not None and age >= 0

    def test_annotations_not_persisted_into_next_row(self, cache_in_tmp):
        """The `_cached` / `_cached_age_min` keys are added AFTER
        cache_set, so they should NOT be written into the cached
        payload. Verified by reading the raw cache row."""
        from alt_data_cache import cache_or_fetch, _CACHE_DB
        cache_or_fetch("insider", "AAPL", lambda s: {"score": 7})
        with closing(sqlite3.connect(_CACHE_DB)) as conn:
            row = conn.execute(
                "SELECT payload_json FROM altdata_cache "
                "WHERE symbol = 'AAPL' AND source = 'insider'"
            ).fetchone()
        persisted = json.loads(row[0])
        assert "_cached" not in persisted, (
            "Annotation leaked into persisted payload — the next read "
            "would inherit a stale annotation and lie about freshness."
        )
        assert "_cached_age_min" not in persisted

    def test_non_dict_payload_passes_through(self, cache_in_tmp):
        """If a fetcher returns None, the wrapper must not raise."""
        from alt_data_cache import cache_or_fetch
        result = cache_or_fetch("insider", "ZZZ", lambda s: None)
        assert result is None

    def test_cache_row_age_minutes_returns_age(self, cache_in_tmp):
        """The helper returns a non-negative age once a row exists."""
        from alt_data_cache import (
            cache_set, _cache_row_age_minutes,
        )
        cache_set("AAPL", "insider", {"score": 5}, ttl_seconds=3600)
        age = _cache_row_age_minutes("AAPL", "insider")
        assert age is not None
        assert 0 <= age < 5, f"Age right after set should be 0; got {age}"

    def test_cache_row_age_minutes_returns_none_for_missing(self, cache_in_tmp):
        from alt_data_cache import _cache_row_age_minutes
        assert _cache_row_age_minutes("NOPE", "insider") is None


# ---------------------------------------------------------------------------
# 5-6. AI prompt freshness summary line
# ---------------------------------------------------------------------------

class TestPromptFreshnessSummary:
    """`_build_batch_prompt` renders a single '[Freshness: X live,
    Y cached (oldest Nh:MMm)]' summary in each candidate's alt-data
    block when any source has the freshness annotation."""

    def _ctx(self):
        return SimpleNamespace(
            max_position_pct=0.10,
            max_total_positions=10,
            enable_short_selling=False,
            segment="stocks",
            signal_weights="{}",
            prompt_layout="{}",
        )

    def _portfolio(self):
        return {
            "equity": 100_000, "cash": 50_000,
            "num_positions": 3,
            "positions": [],
            "drawdown_pct": 0, "drawdown_action": "normal",
        }

    def _market_ctx(self):
        return {"regime": "bull", "vix": 14.0, "political": ""}

    def test_summary_appears_when_alt_data_has_annotations(self):
        from ai_analyst import _build_batch_prompt
        candidates = [{
            "symbol": "AAPL",
            "signal": "BUY", "score": 2, "votes": {"insider_cluster": "BUY"},
            "price": 200.0,
            "alt_data": {
                "insider": {
                    "net_direction": "buying",
                    "recent_buys": 3, "recent_sells": 0,
                    "_cached": False, "_cached_age_min": 0,
                },
                "short": {
                    "short_pct_float": 8.0, "squeeze_risk": "med",
                    "_cached": True, "_cached_age_min": 120,
                },
            },
        }]
        prompt = _build_batch_prompt(
            candidates, self._portfolio(), self._market_ctx(),
            ctx=self._ctx(),
        )
        assert "[Freshness:" in prompt, (
            "Freshness summary line missing. Each candidate's alt-data "
            "block should include a single '[Freshness: X live, Y "
            "cached (oldest Nh:MMm)]' line so the AI can weight signals "
            "by recency."
        )
        # 1 live (insider) + 1 cached (short)
        assert "1 live" in prompt
        assert "1 cached" in prompt
        # Age 120 min = 2h 0m
        assert "2h00m" in prompt

    def test_summary_omitted_when_no_annotations(self):
        """A candidate whose alt-data is empty / unannotated (e.g., the
        sources are disabled for this profile) gets no freshness line —
        nothing to summarize."""
        from ai_analyst import _build_batch_prompt
        candidates = [{
            "symbol": "AAPL",
            "signal": "BUY", "score": 2, "votes": {"insider_cluster": "BUY"},
            "price": 200.0,
            "alt_data": {},
        }]
        prompt = _build_batch_prompt(
            candidates, self._portfolio(), self._market_ctx(),
            ctx=self._ctx(),
        )
        assert "[Freshness:" not in prompt

    def test_age_format_under_one_hour_shows_minutes(self):
        from ai_analyst import _build_batch_prompt
        candidates = [{
            "symbol": "AAPL",
            "signal": "BUY", "score": 2, "votes": {"insider_cluster": "BUY"},
            "price": 200.0,
            "alt_data": {
                "insider": {
                    "net_direction": "buying",
                    "recent_buys": 1, "recent_sells": 0,
                    "_cached": True, "_cached_age_min": 45,
                },
            },
        }]
        prompt = _build_batch_prompt(
            candidates, self._portfolio(), self._market_ctx(),
            ctx=self._ctx(),
        )
        assert "45m" in prompt
        # No "h" prefix for sub-hour ages
        assert "0h" not in prompt
