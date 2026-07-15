"""Universe/candidate breadth mechanics (2026-07-15).

Companions to the rotation-window pins in
test_universe_stratification_2026_07_08.py. Covers the other three
mechanical breadth fixes:

  1. Day-anchored universe freshness — the pure-TTL check drifted the
     daily rebuild to 10:00 ET, so the fleet opened every day on
     YESTERDAY'S universe and swapped candidate pools mid-morning.
  2. Dual-class dedupe — GOOG + GOOGL both held top-30 slots (one
     company, two lines; 222 of 355 held-name shortlist appearances).
  3. Held-name candidate append — a held stock that rotates out of the
     shared window must STILL be scanned (exits must flow; the AI exit
     path produced 75% of the cohort's realized dollars).
"""
from __future__ import annotations

import inspect
import os
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

_ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Day-anchored freshness
# ---------------------------------------------------------------------------

class TestUniverseCacheDayAnchor:
    def test_same_day_cache_is_fresh(self):
        from screener import _universe_cache_fresh
        assert _universe_cache_fresh((time.time() - 3600, ["AAPL"]))

    def test_yesterdays_cache_is_stale_even_inside_24h(self):
        """09:30 ET must not trade on a list built 14:00 ET yesterday —
        that is exactly the drift the TTL-only check allowed."""
        from screener import _universe_cache_fresh
        now_et = datetime.now(_ET)
        yesterday_evening = (now_et - timedelta(days=1)).replace(
            hour=23, minute=0)
        # within 24h of most "now"s, but a different ET calendar day
        assert not _universe_cache_fresh(
            (yesterday_evening.timestamp(), ["AAPL"]))

    def test_past_ttl_is_stale_regardless(self):
        from screener import _universe_cache_fresh
        assert not _universe_cache_fresh(
            (time.time() - 86401, ["AAPL"]))

    def test_garbage_cache_is_stale(self):
        from screener import _universe_cache_fresh
        assert not _universe_cache_fresh(None)
        assert not _universe_cache_fresh(("not-a-ts", []))

    def test_both_validity_checks_use_the_helper(self):
        """Both cache reads in screen_dynamic_universe (pre-lock and
        the single-flight re-check) must go through the day-anchored
        helper — a raw TTL compare re-opens the drift."""
        import screener
        src = inspect.getsource(screener.screen_dynamic_universe)
        assert src.count("_universe_cache_fresh(cached)") == 2
        assert "_DYNAMIC_TTL" not in src, (
            "raw TTL comparison re-introduced in screen_dynamic_universe")


class TestPreOpenUniverseWarm:
    def test_warm_is_wired_into_both_sleep_paths(self):
        """Same reachability class as the pre-open disarm (round-1
        review C1): a market-hours fleet parks in the closed-market
        sleep loop, so the warm must be called from inside it AND from
        the outer not-open branch."""
        import multi_scheduler
        src = inspect.getsource(multi_scheduler.main_loop)
        assert src.count("_preopen_universe_warm(now)") >= 2

    def test_warm_rejects_outside_window(self, monkeypatch):
        import multi_scheduler as ms
        far = datetime.now(_ET)
        monkeypatch.setattr(
            ms, "next_market_open",
            lambda now: far + timedelta(hours=6))
        called = []
        monkeypatch.setattr(ms, "_load_active_profiles",
                            lambda: called.append(1) or [])
        ms._preopen_universe_warm(far)
        assert not called, (
            "outside [open-45m, open) the warm must reject in O(1) "
            "without loading profiles")

    def test_warm_memo_only_set_on_success(self, monkeypatch):
        import multi_scheduler as ms
        now = datetime.now(_ET)
        monkeypatch.setattr(ms, "next_market_open",
                            lambda n: now + timedelta(minutes=30))
        monkeypatch.setattr(
            ms, "_load_active_profiles",
            lambda: [{"id": 1, "market_type": "stocks"}])
        import models
        monkeypatch.setattr(
            models, "build_user_context_from_profile",
            lambda pid: (_ for _ in ()).throw(RuntimeError("db blip")))
        ms._last_universe_warm_date = None
        ms._preopen_universe_warm(now)
        assert ms._last_universe_warm_date is None, (
            "a failed warm must not forfeit the day — the 60s wake-ups "
            "retry for free (same contract as the disarm sweep)")


# ---------------------------------------------------------------------------
# Dual-class dedupe
# ---------------------------------------------------------------------------

class TestDualClassDedupe:
    NAMES = {
        "GOOGL": "Alphabet Inc. Class A Common Stock",
        "GOOG": "Alphabet Inc. Class C Capital Stock",
        "FOX": "Fox Corporation Class B Common Stock",
        "FOXA": "Fox Corporation Class A Common Stock",
        "NVDA": "NVIDIA Corporation Common Stock",
        "AMD": "Advanced Micro Devices, Inc. Common Stock",
    }

    def test_alphabet_collapses_to_the_more_liquid_line(self):
        from screener import _dedupe_dual_class
        results = [("GOOGL", 30e6, 180.0), ("NVDA", 25e6, 120.0),
                   ("GOOG", 20e6, 181.0), ("AMD", 10e6, 150.0)]
        kept = _dedupe_dual_class(results, self.NAMES)
        assert [r[0] for r in kept] == ["GOOGL", "NVDA", "AMD"]

    def test_fox_pair_collapses_too(self):
        from screener import _dedupe_dual_class
        results = [("FOXA", 5e6, 40.0), ("FOX", 4e6, 39.0)]
        kept = _dedupe_dual_class(results, self.NAMES)
        assert [r[0] for r in kept] == ["FOXA"]

    def test_distinct_companies_with_similar_names_survive(self):
        """The ticker-prefix condition is the guard: same normalized
        name alone must NOT merge two different issuers."""
        from screener import _dedupe_dual_class
        names = {"ABCD": "Meridian Group Inc.",
                 "XYZQ": "Meridian Group Inc."}  # coincidence, no prefix
        results = [("ABCD", 5e6, 10.0), ("XYZQ", 4e6, 11.0)]
        kept = _dedupe_dual_class(results, names)
        assert len(kept) == 2

    def test_empty_names_never_match(self):
        from screener import _dedupe_dual_class
        results = [("AAA", 5e6, 10.0), ("AAAB", 4e6, 11.0)]
        kept = _dedupe_dual_class(results, {})
        assert len(kept) == 2

    def test_order_preserved(self):
        from screener import _dedupe_dual_class
        results = [("NVDA", 9e6, 1.0), ("GOOGL", 8e6, 1.0),
                   ("AMD", 7e6, 1.0), ("GOOG", 6e6, 1.0)]
        kept = _dedupe_dual_class(results, self.NAMES)
        assert [r[0] for r in kept] == ["NVDA", "GOOGL", "AMD"]

    def test_wired_into_the_universe_build(self):
        import screener
        src = inspect.getsource(screener._screen_dynamic_universe_locked)
        sort_idx = src.find("results.sort(key=lambda x: x[1] * x[2]")
        dedupe_idx = src.find("_dedupe_dual_class(results, asset_names)")
        strat_idx = src.find("_stratify_by_sector(results[")
        assert 0 < sort_idx < dedupe_idx < strat_idx, (
            "dedupe must run after the dollar-volume sort (so first-"
            "seen = most liquid) and before stratification")


# ---------------------------------------------------------------------------
# Held-name candidate append
# ---------------------------------------------------------------------------

class TestHeldNameAppend:
    def test_held_append_is_per_profile_and_after_the_shared_cache(self):
        """The shared candidate list is fleet-cached; held books differ
        per profile and must be appended AFTER _get_shared_candidates
        returns, never written into the shared cache."""
        import multi_scheduler
        src = inspect.getsource(multi_scheduler._task_scan_and_trade)
        shared_idx = src.find("_get_shared_candidates(ctx, seg, is_crypto)")
        held_idx = src.find("get_virtual_positions(ctx.db_path)")
        assert 0 < shared_idx < held_idx
        assert "symbols = symbols + _added_held" in src
        # and the shared builder itself must not read held positions
        shared_src = inspect.getsource(
            multi_scheduler._get_shared_candidates)
        assert "get_virtual_positions" not in shared_src, (
            "held names in the SHARED cache would leak one profile's "
            "book into every profile's candidate list — isolation "
            "violation")

    def test_held_stock_names_only_options_excluded(self):
        import multi_scheduler
        src = inspect.getsource(multi_scheduler._task_scan_and_trade)
        idx = src.find("_held_syms = [")
        block = src[idx:idx + 400]
        assert 'not p.get("occ_symbol")' in block, (
            "option legs must not be appended — multileg maintenance "
            "owns option exits, the entry scan owns stock exits")


# ---------------------------------------------------------------------------
# Adversarial-review round (2026-07-15) pins
# ---------------------------------------------------------------------------

class TestReviewRoundBreadth:
    def test_warm_memo_requires_a_verifiably_fresh_cache(self, monkeypatch):
        """Review #16: screen_dynamic_universe SWALLOWS build failures
        into a stale-cache return — a non-raising return is not
        success. The memo may only be set when the cache is verifiably
        fresh, else the wake-ups must keep retrying."""
        import multi_scheduler as ms
        import screener
        now = datetime.now(_ET)
        monkeypatch.setattr(ms, "next_market_open",
                            lambda n: now + timedelta(minutes=30))
        monkeypatch.setattr(
            ms, "_load_active_profiles",
            lambda: [{"id": 1, "market_type": "stocks"}])
        import models
        ctx = type("C", (), {"min_price": 10.0, "max_price": 10000.0,
                             "min_volume": 100000, "segment": "stocks"})()
        monkeypatch.setattr(models, "build_user_context_from_profile",
                            lambda pid: ctx)
        monkeypatch.setattr(screener, "screen_dynamic_universe",
                            lambda **kw: ["STALE1", "STALE2"])
        monkeypatch.setattr(screener, "universe_cache_fresh_for",
                            lambda *a, **k: False)
        ms._last_universe_warm_date = None
        ms._preopen_universe_warm(now)
        assert ms._last_universe_warm_date is None, (
            "a stale/fallback return marked the warm done — the fleet "
            "would open on yesterday's universe")
        monkeypatch.setattr(screener, "universe_cache_fresh_for",
                            lambda *a, **k: True)
        ms._preopen_universe_warm(now)
        assert ms._last_universe_warm_date is not None
        ms._last_universe_warm_date = None  # leave module state clean

    def test_bucket_straddling_build_never_caches_a_stale_window(self):
        """Review #17: a build that crosses the 30-min boundary must not
        store bucket N's rotation slice into bucket N+1's cache."""
        import inspect
        import multi_scheduler
        src = inspect.getsource(multi_scheduler._get_shared_candidates)
        assert "if int(_time.time() / 1800) == now_bucket:" in src
        store_idx = src.find("_screener_cache[cache_key] = result")
        guard_idx = src.find("if int(_time.time() / 1800) == now_bucket:")
        assert 0 < guard_idx < store_idx, (
            "the bucket re-check must gate the cache store")
