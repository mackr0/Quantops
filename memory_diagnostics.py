"""Attribute the scheduler's memory growth from the live process.

2026-09-21 — `multi_scheduler.py` grows by roughly 700-800MB over ONE
trading session (system `sar` history: flat overnight, climbing through
13:30-20:00 UTC, the pages cold afterwards and pushed to swap), fills the
1GB swapfile within two days and was OOM-killed at 1.7GB on 2026-08-27.
Three plausible causes were tested and ruled out by measurement before
this module was written: the bars cache (tops out near 200MB for the
whole universe), a new LLM SDK client per call (reclaimed when dropped),
and glibc arena retention under the per-cycle 13-thread pool (RSS
plateaus at +27MB in a faithful replay; capping arenas changes nothing).
The growth only happens in the live trading cycle, so it is measured
there.

What it reports, all at INFO with a `[MEMDIAG]` prefix so the record is
in journald and nothing lands on /issues:

  every call        nothing, unless a report is due (one time check)
  every 5 minutes   one line: RSS, swap, threads, since-start deltas
  every 30 minutes  a GROWTH REPORT vs the first report after start:
                    the object TYPES whose live count grew most, the
                    module-level CONTAINERS whose length grew most, and
                    live counts of the usual suspects (DataFrames,
                    sqlite connections, threads, HTTP clients, locks)

  If RSS grows while no Python type or container does, the memory is
  not held by Python objects (allocator or a C extension) — the report
  says so explicitly, because that changes where to look.

Optional, bounded ALLOCATION TRACE: `touch <repo>/.memdiag_trace` and the
next call starts `tracemalloc` (5 frames); 20 minutes later it logs the
25 call sites whose allocations grew most, stops tracing and removes the
flag. Off by default — tracing costs CPU and memory on a 2GB box.

Never raises: a diagnostic must not be able to hurt the scheduler.
"""
from __future__ import annotations

import gc
import logging
import os
import sys
import threading
import time
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 300
REPORT_SECONDS = 1800
TRACE_SECONDS = 1200
TRACE_FLAG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          ".memdiag_trace")
_CONTAINER_MIN_LEN = 200
_HEAVY_TYPES = ("DataFrame", "Series", "ndarray", "bytes", "bytearray",
                "memoryview", "BlockManager")
_SUSPECT_TYPES = ("DataFrame", "Series", "ndarray", "Connection", "Thread",
                  "Client", "Session", "SSLContext", "lock", "Future",
                  "ThreadPoolExecutor", "UserContext")

_state: Dict[str, Any] = {
    "started": None, "first_rss": None, "last_heartbeat": 0.0,
    "last_report": 0.0, "baseline_types": None,
    "baseline_containers": None, "trace_started": None,
    "trace_baseline": None,
}
_lock = threading.Lock()


def read_memory() -> Dict[str, Optional[float]]:
    """RSS / swap (MB) and thread count from /proc; None where the
    platform has no /proc (macOS dev machines)."""
    out: Dict[str, Optional[float]] = {"rss_mb": None, "swap_mb": None,
                                       "threads": float(threading.active_count())}
    try:
        with open(f"/proc/{os.getpid()}/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    out["rss_mb"] = int(line.split()[1]) / 1024.0
                elif line.startswith("VmSwap:"):
                    out["swap_mb"] = int(line.split()[1]) / 1024.0
    except OSError:
        pass
    return out


def type_counts() -> Counter:
    """Live object count per type name. O(number of objects) — seconds
    on a large heap, which is why it runs every 30 minutes, not every
    cycle."""
    counts: Counter = Counter()
    for obj in gc.get_objects():
        counts[type(obj).__name__] += 1
    return counts


def container_lengths(min_len: int = _CONTAINER_MIN_LEN) -> Dict[str, int]:
    """{module.attribute: len} for module-level dicts / lists / sets /
    deques at least `min_len` long — where an unbounded cache lives."""
    out: Dict[str, int] = {}
    for mod_name, mod in list(sys.modules.items()):
        if mod is None or mod_name.startswith(("_", "encodings")):
            continue
        try:
            attrs = list(vars(mod).items())
        except TypeError:
            continue
        for name, value in attrs:
            if isinstance(value, (dict, list, set, frozenset)) or \
                    type(value).__name__ in ("deque", "OrderedDict",
                                             "defaultdict"):
                try:
                    n = len(value)
                except TypeError:
                    continue
                if n >= min_len:
                    out[f"{mod_name}.{name}"] = n
    return out


def _top_growth(before: Dict[str, int], after: Dict[str, int],
                n: int) -> List[Tuple[str, int, int]]:
    grown = [(k, after[k] - before.get(k, 0), after[k]) for k in after
             if after[k] - before.get(k, 0) > 0]
    grown.sort(key=lambda t: t[1], reverse=True)
    return grown[:n]


def growth_report(baseline_types: Dict[str, int],
                  baseline_containers: Dict[str, int],
                  now_types: Dict[str, int],
                  now_containers: Dict[str, int],
                  rss_delta_mb: Optional[float],
                  native_threshold_mb: float = 100.0) -> List[str]:
    """The report's lines — pure, so it is tested without a live heap."""
    lines: List[str] = []
    types = _top_growth(baseline_types, now_types, 12)
    conts = _top_growth(baseline_containers, now_containers, 12)
    lines.append("object types that grew most (type +delta =now): " + (
        ", ".join(f"{k} +{d:,} ={v:,}" for k, d, v in types) or "none"))
    lines.append("module-level containers that grew most: " + (
        ", ".join(f"{k} +{d:,} ={v:,}" for k, d, v in conts) or "none"))
    lines.append("usual suspects (live count): " + ", ".join(
        f"{t}={now_types.get(t, 0):,}" for t in _SUSPECT_TYPES
        if now_types.get(t, 0)))
    # Object COUNT is a poor proxy for bytes: a few thousand DataFrames
    # or byte buffers can hold hundreds of MB. So the "not Python
    # objects" verdict needs all three to be quiet: overall counts,
    # the buffer-holding types, and the module-level containers.
    objects_grew = sum(max(0, now_types[k] - baseline_types.get(k, 0))
                       for k in now_types)
    heavy_grew = sum(max(0, now_types.get(k, 0) - baseline_types.get(k, 0))
                     for k in _HEAVY_TYPES)
    containers_grew = sum(d for _k, d, _v in conts)
    if (rss_delta_mb is not None and rss_delta_mb > native_threshold_mb
            and objects_grew < 50_000 and heavy_grew < 200
            and containers_grew < 1_000):
        lines.append(
            f"RSS grew {rss_delta_mb:.0f}MB while Python object counts "
            "barely moved — the memory is NOT held by Python objects; "
            "look at the allocator or a C extension (numpy / pandas / "
            "sqlite / ssl buffers), not at a Python cache")
    return lines


def _trace_tick(now: float) -> None:
    import tracemalloc
    if _state["trace_started"] is None:
        if not os.path.exists(TRACE_FLAG):
            return
        tracemalloc.start(5)
        _state["trace_started"] = now
        _state["trace_baseline"] = tracemalloc.take_snapshot()
        logger.info("[MEMDIAG] allocation trace STARTED (flag file seen); "
                    "report in %d minutes", TRACE_SECONDS // 60)
        return
    if now - _state["trace_started"] < TRACE_SECONDS:
        return
    snap = tracemalloc.take_snapshot()
    stats = snap.compare_to(_state["trace_baseline"], "traceback")
    logger.info("[MEMDIAG] allocation trace: top growth sites over %d min",
                TRACE_SECONDS // 60)
    for stat in stats[:25]:
        if stat.size_diff <= 0:
            break
        where = " <- ".join(
            f"{os.path.basename(f.filename)}:{f.lineno}"
            for f in list(stat.traceback)[-4:][::-1])
        logger.info("[MEMDIAG]   +%.1fMB (%+d blocks) %s",
                    stat.size_diff / 1e6, stat.count_diff, where)
    tracemalloc.stop()
    _state["trace_started"] = None
    _state["trace_baseline"] = None
    try:
        os.remove(TRACE_FLAG)
    except OSError as exc:
        logger.warning("[MEMDIAG] could not remove %s (%s) — remove it by "
                       "hand or the trace restarts", TRACE_FLAG, exc)


def tick(now: Optional[float] = None) -> None:
    """Call once per scheduler loop iteration. Cheap unless a report is
    due. Never raises."""
    try:
        now = time.time() if now is None else now
        with _lock:
            if _state["started"] is None:
                _state["started"] = now
                _state["first_rss"] = read_memory()["rss_mb"]
            _trace_tick(now)
            if now - _state["last_heartbeat"] < HEARTBEAT_SECONDS:
                return
            _state["last_heartbeat"] = now
            mem = read_memory()
            rss, first = mem["rss_mb"], _state["first_rss"]
            delta = (rss - first) if (rss is not None and first is not None) else None
            hours = (now - _state["started"]) / 3600.0
            logger.info(
                "[MEMDIAG] rss=%s swap=%s threads=%d uptime=%.1fh rss_delta=%s",
                f"{rss:.0f}MB" if rss is not None else "n/a",
                f"{mem['swap_mb']:.0f}MB" if mem["swap_mb"] is not None else "n/a",
                int(mem["threads"] or 0), hours,
                f"{delta:+.0f}MB" if delta is not None else "n/a")
            if now - _state["last_report"] < REPORT_SECONDS:
                return
            _state["last_report"] = now
            t0 = time.time()
            types = dict(type_counts())
            conts = container_lengths()
            if _state["baseline_types"] is None:
                _state["baseline_types"] = types
                _state["baseline_containers"] = conts
                _state["prev_types"], _state["prev_containers"] = types, conts
                _state["prev_rss"] = rss
                logger.info("[MEMDIAG] baseline taken: %d live objects, %d "
                            "module-level containers >= %d long (%.1fs)",
                            sum(types.values()), len(conts),
                            _CONTAINER_MIN_LEN, time.time() - t0)
                return
            # Two views. SINCE START includes one-time costs — the
            # scheduler imports its heavy modules lazily, on the first
            # trading cycle, long after the baseline. THIS WINDOW (the
            # last 30 minutes) does not: a leak is whatever keeps
            # appearing here, window after window.
            prev_rss = _state.get("prev_rss")
            window = (rss - prev_rss) if (rss is not None
                                          and prev_rss is not None) else None
            logger.info("[MEMDIAG] === THIS WINDOW (last %d min), rss %s ===",
                        REPORT_SECONDS // 60,
                        f"{window:+.0f}MB" if window is not None else "n/a")
            # ~10MB per 5-minute cycle is ~60MB per window: a 30MB
            # window with quiet objects is already worth saying.
            for line in growth_report(_state["prev_types"],
                                      _state["prev_containers"],
                                      types, conts, window,
                                      native_threshold_mb=30.0):
                logger.info("[MEMDIAG] %s", line)
            logger.info("[MEMDIAG] === SINCE START, rss %s ===",
                        f"{delta:+.0f}MB" if delta is not None else "n/a")
            for line in growth_report(_state["baseline_types"],
                                      _state["baseline_containers"],
                                      types, conts, delta):
                logger.info("[MEMDIAG] %s", line)
            _state["prev_types"], _state["prev_containers"] = types, conts
            _state["prev_rss"] = rss
            logger.info("[MEMDIAG] growth report took %.1fs",
                        time.time() - t0)
    except Exception as exc:          # a diagnostic must never hurt the scheduler
        logger.warning("[MEMDIAG] tick failed (%s: %s) — diagnostics only, "
                       "scheduler unaffected", type(exc).__name__, exc)
