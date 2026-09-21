"""2026-09-21 — the scheduler's memory growth is attributed from the live
process.

`multi_scheduler.py` grows ~700-800MB per trading session and fills swap
within two days (OOM-killed at 1.7GB on 2026-08-27). Three plausible
causes were ruled out by measurement; the growth only happens in the live
cycle, so `memory_diagnostics.tick()` measures it there. Pinned: the
report names what grew, says so when the growth is NOT Python objects,
the optional allocation trace is bounded and removes its own flag, and a
diagnostic can never raise into the scheduler.
"""
from __future__ import annotations

import logging
import os
import sys
import types

import pytest

import memory_diagnostics as md

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch, tmp_path):
    monkeypatch.setattr(md, "_state", {
        "started": None, "first_rss": None, "last_heartbeat": 0.0,
        "last_report": 0.0, "baseline_types": None,
        "baseline_containers": None, "trace_started": None,
        "trace_baseline": None})
    monkeypatch.setattr(md, "TRACE_FLAG", str(tmp_path / ".memdiag_trace"))


class TestGrowthReport:
    def test_names_the_types_and_containers_that_grew(self):
        lines = md.growth_report(
            {"dict": 1000, "DataFrame": 10, "str": 5000},
            {"market_data._bars_cache": 300},
            {"dict": 1200, "DataFrame": 9010, "str": 5000, "Thread": 14},
            {"market_data._bars_cache": 4300, "correlation._correlation_cache": 900},
            rss_delta_mb=650.0)
        text = "\n".join(lines)
        assert "DataFrame +9,000 =9,010" in text
        assert text.index("DataFrame") < text.index("dict +200")   # biggest first
        assert "market_data._bars_cache +4,000 =4,300" in text
        assert "correlation._correlation_cache +900 =900" in text
        assert "DataFrame=9,010" in text and "Thread=14" in text
        assert "NOT held by Python objects" not in text

    def test_says_so_when_the_growth_is_not_python_objects(self):
        """RSS up hundreds of MB with flat object counts changes where
        to look — allocator or a C extension, not a Python cache."""
        lines = md.growth_report({"dict": 1000}, {}, {"dict": 1010}, {},
                                 rss_delta_mb=700.0)
        assert any("NOT held by Python objects" in ln for ln in lines)

    def test_small_rss_growth_is_not_called_a_native_leak(self):
        lines = md.growth_report({"dict": 1000}, {}, {"dict": 1010}, {},
                                 rss_delta_mb=20.0)
        assert not any("NOT held by Python objects" in ln for ln in lines)

    def test_no_rss_available_does_not_crash_the_report(self):
        assert md.growth_report({}, {}, {"dict": 5}, {}, rss_delta_mb=None)


class TestCollectors:
    def test_a_growing_module_level_cache_is_found(self, monkeypatch):
        mod = types.ModuleType("fake_leaky_module")
        mod._cache = {i: i for i in range(5000)}
        mod._small = {1: 1}
        monkeypatch.setitem(sys.modules, "fake_leaky_module", mod)
        found = md.container_lengths()
        assert found["fake_leaky_module._cache"] == 5000
        assert "fake_leaky_module._small" not in found

    def test_type_counts_sees_live_objects(self):
        class Marker:
            pass
        keep = [Marker() for _ in range(250)]
        assert md.type_counts()["Marker"] >= 250
        del keep


class TestTick:
    def test_heartbeat_every_5_minutes_report_every_30(self, monkeypatch,
                                                       caplog):
        monkeypatch.setattr(md, "read_memory", lambda: {
            "rss_mb": 200.0, "swap_mb": 0.0, "threads": 3.0})
        monkeypatch.setattr(md, "type_counts", lambda: {"dict": 100})
        monkeypatch.setattr(md, "container_lengths", lambda: {})
        with caplog.at_level(logging.INFO, logger="memory_diagnostics"):
            t = 1_000_000.0
            md.tick(t)                         # heartbeat + baseline
            md.tick(t + 10)                    # nothing due
            md.tick(t + 301)                   # heartbeat only
            md.tick(t + 1801)                  # heartbeat + growth report
        msgs = [r.getMessage() for r in caplog.records]
        assert sum("rss=200MB" in m for m in msgs) == 3
        assert sum("baseline taken" in m for m in msgs) == 1
        assert sum("object types that grew most" in m for m in msgs) == 1

    def test_a_failing_collector_never_reaches_the_scheduler(
            self, monkeypatch, caplog):
        def boom():
            raise MemoryError("collector blew up")
        monkeypatch.setattr(md, "type_counts", boom)
        with caplog.at_level(logging.WARNING, logger="memory_diagnostics"):
            md.tick(1_000_000.0)               # must not raise
        assert any("tick failed" in r.getMessage()
                   and "scheduler unaffected" in r.getMessage()
                   for r in caplog.records)

    def test_works_where_there_is_no_proc(self, monkeypatch):
        monkeypatch.setattr(md, "type_counts", lambda: {"dict": 1})
        monkeypatch.setattr(md, "container_lengths", lambda: {})
        md.tick(1_000_000.0)                   # macOS: rss is None; no raise


class TestAllocationTrace:
    def test_off_by_default(self):
        import tracemalloc
        md.tick(1_000_000.0)
        assert md._state["trace_started"] is None
        assert not tracemalloc.is_tracing()

    def test_flag_starts_a_bounded_trace_that_cleans_up_after_itself(
            self, caplog):
        import tracemalloc
        open(md.TRACE_FLAG, "w").close()
        with caplog.at_level(logging.INFO, logger="memory_diagnostics"):
            md.tick(2_000_000.0)
            assert tracemalloc.is_tracing()
            junk = [bytearray(200_000) for _ in range(20)]     # ~4MB
            md.tick(2_000_000.0 + md.TRACE_SECONDS - 5)        # not yet
            assert tracemalloc.is_tracing()
            md.tick(2_000_000.0 + md.TRACE_SECONDS + 5)        # report
            del junk
        assert not tracemalloc.is_tracing()
        assert not os.path.exists(md.TRACE_FLAG)
        msgs = [r.getMessage() for r in caplog.records]
        assert any("allocation trace STARTED" in m for m in msgs)
        assert any("top growth sites" in m for m in msgs)
        assert any("test_memory_diagnostics" in m and "MB" in m
                   for m in msgs), "the growth site must be named"


class TestWiring:
    def test_the_scheduler_loop_calls_tick_every_iteration(self):
        src = open(os.path.join(REPO, "multi_scheduler.py")).read()
        loop = src[src.index("    while not _shutdown:"):]
        head = loop[:1200]
        assert "memory_diagnostics.tick()" in head
        assert head.index("memory_diagnostics.tick()") < head.index(
            "# Rotate log file if day changed")

    def test_reports_stay_off_the_issues_page(self):
        """INFO only for the routine lines — /issues collects WARNING+."""
        src = open(os.path.join(REPO, "memory_diagnostics.py")).read()
        routine = [ln for ln in src.splitlines()
                   if "[MEMDIAG]" in ln and "logger." in ln]
        assert routine
        for ln in routine:
            if "tick failed" in ln or "could not remove" in ln:
                continue
            assert "logger.info(" in ln, ln
