"""2026-11-01 — scheduler memory: what the live malloc_trim experiment
settled, and the three things shipped on it.

Six weeks of 30-minute reports: during a session `malloc_trim(0)` gives
back ~130MB (freed by Python, kept by glibc) and ~670MB stays held — a
native holder, still being attributed. Shipped now:
  1. the ~130MB is reclaimed continuously (trim on any heartbeat that
     shows 40MB of growth since the last trim);
  2. when the process holds too much (600MB swap, or 1.1GB rss+swap)
     `restart_recommended()` says so and the scheduler re-executes
     itself in place from its closed-market loop, only with >60 min to
     the open — so swap can no longer fill over 2-3 days and end in a
     mid-session OOM kill;
  3. `order_status_cache` is bounded (4,709 entries after two days,
     never evicted, was one of the two small leaks named 09-19).
"""
from __future__ import annotations

import os
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, REPO)


class TestOrderCacheIsBounded:
    def test_oldest_tenth_evicted_above_cap(self):
        import order_status_cache as osc
        osc.clear_cache()
        with osc._lock:
            for i in range(osc.CACHE_MAX_ENTRIES + 1):
                osc._cache[(f"o{i}", False)] = (float(i), object(), True)
            evicted = osc._evict_if_needed_locked()
        assert evicted == (osc.CACHE_MAX_ENTRIES + 1) // 10
        assert len(osc._cache) == osc.CACHE_MAX_ENTRIES + 1 - evicted
        # the oldest went, the newest stayed
        assert ("o0", False) not in osc._cache
        assert (f"o{osc.CACHE_MAX_ENTRIES}", False) in osc._cache
        osc.clear_cache()

    def test_under_cap_nothing_evicted(self):
        import order_status_cache as osc
        osc.clear_cache()
        with osc._lock:
            osc._cache[("a", False)] = (1.0, object(), True)
            assert osc._evict_if_needed_locked() == 0
        osc.clear_cache()

    def test_eviction_is_wired_into_the_setter(self):
        src = open(os.path.join(REPO, "order_status_cache.py")).read()
        i = src.index("_cache[key] = (now, order, _is_immutable(order))")
        assert "_evict_if_needed_locked()" in src[i:i + 200]


class TestHeartbeatTrim:
    def test_trims_only_after_growth(self, monkeypatch):
        import memory_diagnostics as md
        calls = []
        monkeypatch.setattr(md, "trim_allocator", lambda: calls.append(1) or (300.0, 250.0))
        md._state["last_trim_rss"] = None
        assert md.maybe_trim(200.0) is None          # first reading = baseline
        assert md.maybe_trim(200.0 + md.TRIM_GROWTH_MB - 1) is None
        assert calls == []
        assert md.maybe_trim(200.0 + md.TRIM_GROWTH_MB) == (300.0, 250.0)
        assert calls == [1]
        assert md._state["last_trim_rss"] == 250.0   # next trim measured from the trimmed size
        md._state["last_trim_rss"] = None

    def test_no_reading_no_trim(self):
        import memory_diagnostics as md
        md._state["last_trim_rss"] = None
        assert md.maybe_trim(None) is None


class TestRestartRecommendation:
    def test_thresholds(self):
        import memory_diagnostics as md
        assert md.restart_recommended({"rss_mb": 400.0, "swap_mb": 100.0}) is None
        assert "swap" in md.restart_recommended({"rss_mb": 300.0, "swap_mb": md.RESTART_SWAP_MB})
        assert "rss+swap" in md.restart_recommended(
            {"rss_mb": md.RESTART_TOTAL_MB - 50.0, "swap_mb": 50.0})
        assert md.restart_recommended({"rss_mb": None, "swap_mb": 900.0}) is None  # unverifiable → no action

    def test_scheduler_reexecs_only_in_the_closed_loop_with_an_hour_to_spare(self):
        src = open(os.path.join(REPO, "multi_scheduler.py")).read()
        i = src.index("MEMORY SELF-RESTART (2026-11-01)")
        block = src[i:i + 1800]
        assert "memory_diagnostics.restart_recommended()" in block
        assert "is_market_open(" in block and "timedelta(minutes=60)" in block, (
            "the re-exec must be gated on the market staying closed for the next hour")
        assert "os.execv(sys.executable, [sys.executable] + sys.argv)" in block
        # it lives inside the closed-market sleep loop, after housekeeping
        j = src.index("_closed_market_housekeeping()\n", 0)
        assert j < i < src.index("_preopen_disarm_sweep(now)", i)

    def test_sampler_script_exists_and_parses(self):
        import subprocess
        path = os.path.join(REPO, "scripts", "memdiag_native_sampler.sh")
        assert os.path.exists(path)
        assert subprocess.run(["bash", "-n", path], capture_output=True).returncode == 0
