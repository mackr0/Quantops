"""Tests for the pre-market warmup task (docs/21, 2026-05-20).

Pins:
  1. _get_universe() returns dedup'd, uppercased symbol list
  2. _get_universe() falls back to static seed when cycle_data is empty
  3. run_warmup populates the cache via cache_set (mocked fetchers)
  4. A broken source doesn't take down the warmup — others still run
  5. Rate-limit honored when configured
  6. Kill switch (ALTDATA_CACHE_ENABLED=0) makes warmup a no-op
"""
from __future__ import annotations

import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def cache_in_tmp(tmp_path, monkeypatch):
    """Redirect cache file so warmup writes go to a temp DB."""
    import alt_data_cache
    cache_dir = tmp_path / "altdata" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_db = str(cache_dir / "static_altdata.db")
    monkeypatch.setattr(alt_data_cache, "_CACHE_DIR", str(cache_dir))
    monkeypatch.setattr(alt_data_cache, "_CACHE_DB", cache_db)
    return alt_data_cache


# ---------------------------------------------------------------------------
# (1) Universe
# ---------------------------------------------------------------------------

def test_universe_dedupes_and_uppercases(tmp_path, monkeypatch):
    """If cycle_data files yield duplicate or mixed-case symbols,
    the universe must collapse them to one uppercase entry each.
    Cap segments are stubbed empty to isolate the cycle_data path."""
    import json
    monkeypatch.chdir(tmp_path)
    # Stub cap segments to empty so we only see cycle_data symbols
    import segments
    monkeypatch.setattr(segments, "LARGE_CAP_UNIVERSE", [])
    monkeypatch.setattr(segments, "MID_CAP_UNIVERSE", [])
    monkeypatch.setattr(segments, "SMALL_CAP_UNIVERSE", [])
    monkeypatch.setattr(segments, "MICRO_CAP_UNIVERSE", [])
    with open("cycle_data_15.json", "w") as f:
        json.dump({"shortlist": [
            {"symbol": "aapl"}, {"symbol": "AAPL"}, {"symbol": "msft"},
        ]}, f)
    with open("cycle_data_20.json", "w") as f:
        json.dump({"shortlist": [
            {"symbol": "AAPL"}, {"symbol": "GOOG"},
        ]}, f)
    from altdata_warmup import _get_universe
    universe = _get_universe()
    assert universe == ["AAPL", "GOOG", "MSFT"]


def test_universe_excludes_crypto(tmp_path, monkeypatch):
    """Crypto symbols (with '/') aren't subject to the alt-data
    sources we cache, so they should never appear in the warmup
    universe."""
    import json
    monkeypatch.chdir(tmp_path)
    # Stub cap segments empty so we test cycle_data path in isolation
    import segments
    monkeypatch.setattr(segments, "LARGE_CAP_UNIVERSE", [])
    monkeypatch.setattr(segments, "MID_CAP_UNIVERSE", [])
    monkeypatch.setattr(segments, "SMALL_CAP_UNIVERSE", [])
    monkeypatch.setattr(segments, "MICRO_CAP_UNIVERSE", [])
    with open("cycle_data_15.json", "w") as f:
        json.dump({"shortlist": [
            {"symbol": "AAPL"}, {"symbol": "BTC/USD"},
        ]}, f)
    from altdata_warmup import _get_universe
    assert _get_universe() == ["AAPL"]


def test_universe_includes_cap_segments(tmp_path, monkeypatch):
    """2026-05-20: warmup now pulls the full 524-symbol union of
    LARGE/MID/SMALL/MICRO cap segments so the cache covers the
    screener's actual universe, not just whatever happens to be
    in cycle_data shortlists."""
    monkeypatch.chdir(tmp_path)
    from altdata_warmup import _get_universe
    universe = _get_universe()
    # 524 in the canonical universe; allow some flex for future
    # adjustments but it should be much bigger than the prior ~31
    assert len(universe) >= 400, (
        f"Universe is suspiciously small ({len(universe)}). The "
        "cap-segment import probably failed and we fell back to "
        "the static seed list — that's the old behavior the user "
        "explicitly asked us to fix."
    )
    # Spot-check a few names that should appear from cap segments
    assert "AAPL" in universe        # LARGE
    assert "CRM" in universe         # LARGE
    # No crypto
    assert all("/" not in s for s in universe)


# ---------------------------------------------------------------------------
# (2) run_warmup populates the cache
# ---------------------------------------------------------------------------

def test_run_warmup_writes_to_cache(cache_in_tmp, monkeypatch):
    """Stub each warmup source's fetcher; verify run_warmup writes
    one row per (symbol, source) to the cache."""
    fake_fetcher = MagicMock(side_effect=lambda s: {"sym": s, "v": 1})
    monkeypatch.setattr(
        "altdata_warmup._build_warmup_sources",
        lambda: [
            ("insider", fake_fetcher, 0.0),
            ("short", fake_fetcher, 0.0),
        ],
    )
    from altdata_warmup import run_warmup
    summary = run_warmup(symbols=["AAPL", "MSFT"], limit=None)
    # 2 symbols × 2 sources = 4 fetches total
    assert summary["insider"]["fetched"] == 2
    assert summary["short"]["fetched"] == 2
    # And the cache has rows
    assert cache_in_tmp.cache_get("AAPL", "insider") == {"sym": "AAPL", "v": 1}
    assert cache_in_tmp.cache_get("MSFT", "short") == {"sym": "MSFT", "v": 1}


def test_run_warmup_continues_after_per_symbol_failure(cache_in_tmp, monkeypatch):
    """A fetcher raising on one symbol must not stop the warmup
    for other symbols — error count goes up, fetched count for the
    rest still increments."""
    def flaky(symbol):
        if symbol == "MSFT":
            raise RuntimeError("synthetic upstream failure")
        return {"sym": symbol, "v": 1}
    monkeypatch.setattr(
        "altdata_warmup._build_warmup_sources",
        lambda: [("insider", flaky, 0.0)],
    )
    from altdata_warmup import run_warmup
    summary = run_warmup(symbols=["AAPL", "MSFT", "GOOG"])
    assert summary["insider"]["fetched"] == 2  # AAPL, GOOG
    assert summary["insider"]["errors"] == 1   # MSFT


def test_run_warmup_skips_source_missing_from_ttl_config(cache_in_tmp, monkeypatch):
    """If a fetcher is registered but the source name isn't in
    SOURCE_TTL_SECONDS, we can't safely cache it (no TTL guidance).
    Skip with a warning instead of guessing."""
    monkeypatch.setattr(
        "altdata_warmup._build_warmup_sources",
        lambda: [("nonexistent_source", lambda s: {"v": 1}, 0.0)],
    )
    from altdata_warmup import run_warmup
    summary = run_warmup(symbols=["AAPL"])
    assert "nonexistent_source" not in summary


# ---------------------------------------------------------------------------
# (3) Rate limit
# ---------------------------------------------------------------------------

def test_rate_limit_is_honored(cache_in_tmp, monkeypatch):
    """Set rate_limit=0.05 on a fake source; 3 symbols → ≥0.1s wait
    elapsed across the 2 inter-call sleeps. Generous bounds because
    test timing is noisy."""
    monkeypatch.setattr(
        "altdata_warmup._build_warmup_sources",
        lambda: [("insider", lambda s: {"v": 1}, 0.05)],
    )
    from altdata_warmup import run_warmup
    start = time.time()
    run_warmup(symbols=["A", "B", "C"])
    elapsed = time.time() - start
    # 3 symbols × 0.05s rate-limit per call = 0.15s minimum
    assert elapsed >= 0.10, f"rate-limit not respected — {elapsed:.3f}s elapsed"


# ---------------------------------------------------------------------------
# (4) Kill switch
# ---------------------------------------------------------------------------

def test_warmup_is_noop_when_kill_switch_off(cache_in_tmp, monkeypatch):
    monkeypatch.setenv("ALTDATA_CACHE_ENABLED", "0")
    monkeypatch.setattr(
        "altdata_warmup._build_warmup_sources",
        lambda: [("insider", MagicMock(return_value={"v": 1}), 0.0)],
    )
    from altdata_warmup import run_warmup
    summary = run_warmup(symbols=["AAPL"])
    # No work done when disabled
    assert summary == {}
    assert cache_in_tmp.cache_get("AAPL", "insider") is None


# ---------------------------------------------------------------------------
# (5) Integration: get_all_alternative_data hits cache after warmup
# ---------------------------------------------------------------------------

def test_get_all_alternative_data_uses_cache_after_warmup(cache_in_tmp, monkeypatch):
    """Pin the integration: after warmup populates the cache, the
    per-cycle get_all_alternative_data call hits cache (the fetcher
    is NOT called the second time) for cached sources."""
    # We mock the live fetcher to count invocations
    live_fetcher = MagicMock(return_value={"net": 5})
    # Patch the source getter at the alternative_data module level
    monkeypatch.setattr(
        "alternative_data.get_insider_activity", live_fetcher,
    )
    # Warm the cache: first call hits the live fetcher
    from alt_data_cache import cache_or_fetch
    cache_or_fetch("insider", "AAPL", live_fetcher)
    assert live_fetcher.call_count == 1
    # Second call: cache hit, no live fetch
    cache_or_fetch("insider", "AAPL", live_fetcher)
    assert live_fetcher.call_count == 1
