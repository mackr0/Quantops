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

    def test_the_live_case_344mb_with_141_new_frames_is_called_native(self):
        """2026-09-21, second window of the first session: RSS +344MB,
        DataFrame +141, a few thousand dicts and lists. The count-based
        guard read 141 frames as a possible explanation and stayed
        silent; measured in BYTES they held almost nothing."""
        before = {"dict": 62_397, "list": 38_264, "DataFrame": 1_767,
                  "NumpyBlock": 11_413, "BlockValuesRefs": 14_946}
        after = {"dict": 64_252, "list": 39_894, "DataFrame": 1_908,
                 "NumpyBlock": 12_399, "BlockValuesRefs": 16_214}
        lines = md.growth_report(before, {"market_data._bars_cache": 1_227},
                                 after, {"market_data._bars_cache": 1_328},
                                 rss_delta_mb=344.0, native_threshold_mb=30.0,
                                 array_mb_delta=2.0)
        text = "\n".join(lines)
        assert "arrays held by pandas frames: +2MB" in text
        assert "NOT held by Python objects" in text

    def test_when_arrays_do_hold_the_growth_it_is_not_called_native(self):
        lines = md.growth_report({"DataFrame": 10}, {}, {"DataFrame": 151}, {},
                                 rss_delta_mb=344.0, native_threshold_mb=30.0,
                                 array_mb_delta=310.0)
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

    def test_frame_bytes_are_found_and_shared_buffers_counted_once(self):
        """numpy arrays are not tracked by the garbage collector, so a
        walk of gc.get_objects() never sees one (the first version of
        this measure did exactly that and reported zero). The pandas
        blocks that hold them ARE tracked."""
        import numpy as np
        import pandas as pd
        base = md.array_bytes_mb()["owned_arrays_mb"]
        df = pd.DataFrame(np.zeros((1_000_000, 4)))           # 32MB
        now = md.array_bytes_mb()["owned_arrays_mb"]
        assert 30 <= now - base <= 40, now - base
        slices = [df.iloc[i:] for i in range(1, 40)]          # views of it
        again = md.array_bytes_mb()["owned_arrays_mb"]
        assert again - now < 2, "views must not be counted again"
        del slices, df

    def test_trim_returns_a_before_after_pair_or_none(self):
        out = md.trim_allocator()
        assert out is None or (len(out) == 2 and out[1] <= out[0] + 50)

    def test_a_grown_process_runs_the_trim_experiment_and_says_what_it_means(
            self, monkeypatch, caplog):
        heap = {"rss": 100.0}
        monkeypatch.setattr(md, "read_memory", lambda: {
            "rss_mb": heap["rss"], "swap_mb": 0.0, "threads": 2.0})
        monkeypatch.setattr(md, "type_counts", lambda: {"dict": 1000})
        monkeypatch.setattr(md, "container_lengths", lambda: {})
        monkeypatch.setattr(md, "array_bytes_mb", lambda: {
            "owned_arrays_mb": 20.0, "owned_arrays": 5.0, "bytes_mb": 1.0})
        monkeypatch.setattr(md, "trim_allocator", lambda: (900.0, 310.0))
        t = 5_000_000.0
        md.tick(t)
        with caplog.at_level(logging.INFO, logger="memory_diagnostics"):
            heap["rss"] = 900.0
            md.tick(t + 1801)
            heap["rss"] = 330.0                     # next window: measured
            md.tick(t + 3602)                       # from AFTER the trim
        msgs = [r.getMessage() for r in caplog.records]
        trim = [m for m in msgs if "allocator trim" in m]
        assert "900MB -> 310MB (-590MB)" in trim[0]
        assert "FREED by Python" in trim[0]
        assert any("pandas frames hold 20MB in 5 distinct buffers" in m
                   for m in msgs)
        second = [m for m in msgs if "THIS WINDOW" in m][1]
        assert "rss +20MB" in second, second        # 330 - 310, not 330 - 900

    def test_a_trim_that_recovers_nothing_says_the_memory_is_still_held(
            self, monkeypatch, caplog):
        monkeypatch.setattr(md, "read_memory", lambda: {
            "rss_mb": 900.0, "swap_mb": 0.0, "threads": 2.0})
        monkeypatch.setattr(md, "type_counts", lambda: {"dict": 1000})
        monkeypatch.setattr(md, "container_lengths", lambda: {})
        monkeypatch.setattr(md, "array_bytes_mb", lambda: {
            "owned_arrays_mb": 20.0, "owned_arrays": 5.0, "bytes_mb": 1.0})
        monkeypatch.setattr(md, "trim_allocator", lambda: (900.0, 895.0))
        md._state["first_rss"] = 100.0
        md._state["started"] = 6_000_000.0
        t = 6_000_000.0
        md.tick(t)
        with caplog.at_level(logging.INFO, logger="memory_diagnostics"):
            md.tick(t + 1801)
        assert any("still holding" in r.getMessage() for r in caplog.records)

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
        # one report = two views: this window, and since start
        assert sum("object types that grew most" in m for m in msgs) == 2
        assert sum("THIS WINDOW" in m for m in msgs) == 1
        assert sum("SINCE START" in m for m in msgs) == 1

    def test_the_window_view_separates_a_one_time_burst_from_a_leak(
            self, monkeypatch, caplog):
        """The scheduler imports its heavy modules lazily, on the first
        trading cycle — long after the baseline. SINCE START carries
        that one-time burst forever; THIS WINDOW shows only what kept
        growing, which is the leak."""
        heap = {"types": {"function": 1_000, "LeakyThing": 0}, "rss": 100.0}
        monkeypatch.setattr(md, "read_memory", lambda: {
            "rss_mb": heap["rss"], "swap_mb": 0.0, "threads": 2.0})
        monkeypatch.setattr(md, "type_counts", lambda: dict(heap["types"]))
        monkeypatch.setattr(md, "container_lengths", lambda: {})
        t = 3_000_000.0
        md.tick(t)                                           # baseline
        heap["types"] = {"function": 400_000, "LeakyThing": 5_000}
        heap["rss"] = 400.0                                  # imports + leak
        md.tick(t + 1801)
        with caplog.at_level(logging.INFO, logger="memory_diagnostics"):
            heap["types"] = {"function": 400_000, "LeakyThing": 10_000}
            heap["rss"] = 460.0                              # leak only
            md.tick(t + 3602)
        msgs = [r.getMessage() for r in caplog.records]
        w = msgs.index(next(m for m in msgs if "THIS WINDOW" in m))
        s = msgs.index(next(m for m in msgs if "SINCE START" in m))
        assert "rss +60MB" in msgs[w] and "rss +360MB" in msgs[s]
        window_types = msgs[w + 1]
        assert "LeakyThing +5,000" in window_types
        assert "function" not in window_types       # the burst is gone
        assert "function +399,000" in msgs[s + 1]   # …but still in the total

    def test_a_quiet_window_with_growing_rss_gets_the_native_verdict(
            self, monkeypatch, caplog):
        heap = {"rss": 100.0}
        monkeypatch.setattr(md, "read_memory", lambda: {
            "rss_mb": heap["rss"], "swap_mb": 0.0, "threads": 2.0})
        monkeypatch.setattr(md, "type_counts", lambda: {"dict": 1000})
        monkeypatch.setattr(md, "container_lengths", lambda: {})
        t = 4_000_000.0
        md.tick(t)
        with caplog.at_level(logging.INFO, logger="memory_diagnostics"):
            heap["rss"] = 160.0                     # +60MB, objects flat
            md.tick(t + 1801)
        msgs = [r.getMessage() for r in caplog.records]
        w = msgs.index(next(m for m in msgs if "THIS WINDOW" in m))
        s = msgs.index(next(m for m in msgs if "SINCE START" in m))
        assert any("NOT held by Python objects" in m for m in msgs[w:s]), (
            "60MB in one window with quiet objects must be called out — "
            "the since-start threshold (100MB) would miss it for hours")

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

    def test_the_closed_market_sleep_loop_calls_tick_too(self):
        """A market-hours fleet spends every night and weekend parked in
        the inner sleep loop; with the hook only at the top of the outer
        loop it reported once at startup and then nothing for 8 hours.
        The overnight readings are the control, and the last one before
        the open is the baseline the session is read against."""
        src = open(os.path.join(REPO, "multi_scheduler.py")).read()
        i = src.index('f"Market closed, sleeping until')
        inner = src[i:src.index("time.sleep(60)", i)]
        assert "while not _shutdown:" in inner
        assert "memory_diagnostics.tick()" in inner
        assert inner.index("memory_diagnostics.tick()") < inner.index(
            "_closed_market_housekeeping()")

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
