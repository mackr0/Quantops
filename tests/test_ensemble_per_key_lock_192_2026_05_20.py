"""Per-key ensemble lock — #192 carryover from the JSON-mode / cycle-time
fix (2026-05-20).

Pre-#192, `_get_shared_ensemble` ran its entire body under a single
module-global `_ensemble_lock`. Profiles in the same segment correctly
shared the cache, BUT every subsequent caller had to ACQUIRE the lock
to even CHECK the cache. When the lock-holder was mid-AI-call (e.g.,
5 min during the 2026-05-20 Gemini-degraded incident), every other
caller blocked for the full duration. py-spy traces showed up to 12
profiles serialized behind a single slow Gemini call.

Fix structure:
  1. L1 cache check — NO lock (Python GIL guarantees atomic dict read)
  2. L2 cache (SQLite) check — NO lock (WAL-mode concurrent reads)
  3. Cache miss → acquire per-key lock (different keys don't block)
  4. Inside per-key lock: double-check L1, compute if still miss

This file pins:
  1. Module exposes _per_key_ensemble_locks dict + meta lock helper
  2. _get_per_key_ensemble_lock returns the SAME lock for the same key
     (creating once + caching), DIFFERENT locks for different keys
  3. Source body of _get_shared_ensemble has L1 check OUTSIDE the lock
  4. Two threads with DIFFERENT cache keys don't block each other
  5. Two threads with the SAME cache key, with one mid-compute, the
     second eventually returns the same cached result (no duplicate
     AI call)
  6. test_silent_failures.test_ensemble_cache_has_lock continues to
     pass (the substring "_ensemble_lock" remains in the function body
     via the rotation guard)
"""
from __future__ import annotations

import os
import sys
import threading
import time as _time
from unittest.mock import patch

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


# ---------------------------------------------------------------------------
# 1. Module exposes the per-key lock infrastructure
# ---------------------------------------------------------------------------

class TestModuleExposure:
    def test_per_key_lock_dict_exists(self):
        import trade_pipeline as tp
        assert hasattr(tp, "_per_key_ensemble_locks"), (
            "Module must expose _per_key_ensemble_locks dict so locks "
            "are per-cache-key, not global."
        )
        assert isinstance(tp._per_key_ensemble_locks, dict)

    def test_get_per_key_ensemble_lock_helper_exists(self):
        import trade_pipeline as tp
        assert callable(getattr(tp, "_get_per_key_ensemble_lock", None)), (
            "Helper _get_per_key_ensemble_lock must exist to atomically "
            "create-and-cache per-key locks."
        )

    def test_meta_lock_still_named_ensemble_lock(self):
        """test_silent_failures.test_ensemble_cache_has_lock greps the
        source for `_ensemble_lock` or `Lock`. The meta lock keeps the
        legacy name so that guardrail stays useful."""
        import trade_pipeline as tp
        assert hasattr(tp, "_ensemble_lock")


# ---------------------------------------------------------------------------
# 2. Lock helper returns same lock for same key, different for different
# ---------------------------------------------------------------------------

class TestPerKeyLockSemantics:
    def test_same_key_returns_same_lock_object(self):
        from trade_pipeline import _get_per_key_ensemble_lock
        a = _get_per_key_ensemble_lock("stocks")
        b = _get_per_key_ensemble_lock("stocks")
        assert a is b, (
            "Two calls with the same cache key must return the SAME "
            "lock — otherwise two callers could simultaneously enter "
            "the slow path and run duplicate AI calls."
        )

    def test_different_keys_return_different_locks(self):
        from trade_pipeline import _get_per_key_ensemble_lock
        a = _get_per_key_ensemble_lock("stocks_192_test")
        b = _get_per_key_ensemble_lock("crypto_192_test")
        assert a is not b, (
            "Different cache keys must get different locks — that's "
            "the whole point of the per-key refactor (so two segments "
            "don't serialize on each other's AI calls)."
        )

    def test_lock_creation_is_thread_safe(self):
        """Race many threads creating locks for the same fresh key.
        All must end up with the same lock object."""
        from trade_pipeline import _get_per_key_ensemble_lock, _per_key_ensemble_locks
        key = f"race_test_{_time.time_ns()}"
        # Pre-clear to ensure a fresh key
        _per_key_ensemble_locks.pop(key, None)

        locks_seen = []
        barrier = threading.Barrier(20)

        def worker():
            barrier.wait()
            locks_seen.append(_get_per_key_ensemble_lock(key))

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(set(id(l) for l in locks_seen)) == 1, (
            "Race to create a fresh per-key lock produced multiple "
            "lock objects — _get_per_key_ensemble_lock isn't safely "
            "serialized under the meta lock."
        )


# ---------------------------------------------------------------------------
# 3. Source structure: L1 fast-path is OUTSIDE the lock
# ---------------------------------------------------------------------------

class TestFastPathStructure:
    def test_l1_check_outside_lock(self):
        """The whole point of the refactor: L1 cache check happens
        BEFORE the per-key lock acquisition. Pinned via source-string
        inspection."""
        import inspect
        from trade_pipeline import _get_shared_ensemble
        src = inspect.getsource(_get_shared_ensemble)
        # Find the position of the L1 check and the slow-path lock acquire
        l1_check_idx = src.find("if cache_key in _ensemble_cache:")
        slow_path_idx = src.find("key_lock = _get_per_key_ensemble_lock")
        assert l1_check_idx > 0 and slow_path_idx > 0, (
            "Source structure changed — both the L1 check and the "
            "per-key lock acquisition should be present in the function."
        )
        assert l1_check_idx < slow_path_idx, (
            "L1 check must appear BEFORE the per-key lock acquisition "
            "in the source — that's the fast path that lets cache hits "
            "skip the lock entirely."
        )

    def test_double_check_inside_lock(self):
        """Inside the per-key lock, there must be a re-check of L1.
        Without it, two threads racing past the outer L1 check could
        both call the AI (defeats the cache)."""
        import inspect
        from trade_pipeline import _get_shared_ensemble
        src = inspect.getsource(_get_shared_ensemble)
        # Count L1 checks — should be 2: one outside lock, one inside
        n_l1_checks = src.count("if cache_key in _ensemble_cache:")
        assert n_l1_checks >= 2, (
            "The classical double-checked-locking pattern needs two "
            "L1 cache checks — one OUTSIDE the lock (fast path) and "
            "one INSIDE (in case another thread filled while we waited "
            "to acquire). Found %d." % n_l1_checks
        )


# ---------------------------------------------------------------------------
# 4. Behaviorally: different-key threads don't block each other
# ---------------------------------------------------------------------------

class TestNoCrossKeyBlocking:
    """When thread A is mid-AI-call for cache key X, thread B for key Y
    must NOT block on A's lock — they have different keys."""

    def test_b_completes_while_a_holds(self, monkeypatch):
        """Stub run_ensemble for key A to sleep; assert thread B for key
        Y completes within a tight bound, proving B doesn't wait for A."""
        from trade_pipeline import _get_shared_ensemble, _per_key_ensemble_locks
        # Clear any existing cache to force computes
        import trade_pipeline as tp
        tp._ensemble_cache.clear()
        _per_key_ensemble_locks.pop("stocks_no_cross_block", None)
        _per_key_ensemble_locks.pop("crypto_no_cross_block", None)

        # Build a ctx-like object — keep this minimal; the real ctx has
        # ai_provider/ai_model/ai_api_key for run_ensemble to consume.
        class _Ctx:
            ai_provider = "anthropic"
            ai_model = "claude-haiku-4-5-20251001"
            ai_api_key = "fake"
        ctx_a = _Ctx(); ctx_a.segment = "stocks_no_cross_block"
        ctx_b = _Ctx(); ctx_b.segment = "crypto_no_cross_block"

        a_compute_started = threading.Event()
        a_compute_release = threading.Event()
        b_done = threading.Event()
        timing = {}

        def fake_run_ensemble(candidates_data, ctx, **kwargs):
            if ctx.segment == "stocks_no_cross_block":
                a_compute_started.set()
                # Wait until B has had a chance to complete (or 5s elapsed)
                a_compute_release.wait(timeout=5.0)
                return {"verdict": "BUY", "key": "A"}
            else:
                return {"verdict": "HOLD", "key": "B"}

        monkeypatch.setattr("ensemble.run_ensemble", fake_run_ensemble)
        # Also stub the shared_ai_cache so the L2 lookup doesn't surprise
        monkeypatch.setattr("shared_ai_cache.get", lambda *a, **kw: None)
        monkeypatch.setattr("shared_ai_cache.put", lambda *a, **kw: None)

        def run_a():
            t0 = _time.time()
            _get_shared_ensemble([], ctx_a)
            timing["a_elapsed"] = _time.time() - t0

        def run_b():
            t0 = _time.time()
            _get_shared_ensemble([], ctx_b)
            timing["b_elapsed"] = _time.time() - t0
            b_done.set()

        thread_a = threading.Thread(target=run_a)
        thread_b = threading.Thread(target=run_b)
        thread_a.start()
        # Wait until A is definitely mid-compute (holding its per-key lock)
        assert a_compute_started.wait(timeout=2.0), "Thread A never started compute"
        # Now B should be able to run unblocked
        thread_b.start()
        # B should complete quickly even though A is still mid-compute
        assert b_done.wait(timeout=2.0), (
            "Thread B (different cache key) blocked on thread A's per-key "
            "lock — the per-key refactor isn't actually isolating keys."
        )
        # Release A
        a_compute_release.set()
        thread_a.join(timeout=5.0)
        thread_b.join(timeout=1.0)

        assert timing["b_elapsed"] < 1.0, (
            f"Thread B took {timing['b_elapsed']:.2f}s (expected <1s). "
            "Different cache keys should not serialize."
        )


# ---------------------------------------------------------------------------
# 5. Same-key callers cooperate (no duplicate AI call)
# ---------------------------------------------------------------------------

class TestSameKeyDeduplication:
    """When two threads with the SAME cache key both call _get_shared_ensemble
    simultaneously on a cold cache, exactly ONE run_ensemble call should
    happen. The second thread should wait, then hit the cache."""

    def test_only_one_ai_call_for_same_key(self, monkeypatch):
        from trade_pipeline import _get_shared_ensemble, _per_key_ensemble_locks
        import trade_pipeline as tp
        tp._ensemble_cache.clear()
        _per_key_ensemble_locks.pop("same_key_dedup", None)

        call_count = {"n": 0}
        compute_started = threading.Event()
        compute_release = threading.Event()

        def fake_run_ensemble(candidates_data, ctx, **kwargs):
            call_count["n"] += 1
            compute_started.set()
            compute_release.wait(timeout=5.0)
            return {"verdict": "BUY"}

        monkeypatch.setattr("ensemble.run_ensemble", fake_run_ensemble)
        monkeypatch.setattr("shared_ai_cache.get", lambda *a, **kw: None)
        monkeypatch.setattr("shared_ai_cache.put", lambda *a, **kw: None)

        class _Ctx:
            ai_provider = "anthropic"
            ai_model = "claude-haiku-4-5-20251001"
            ai_api_key = "fake"
            segment = "same_key_dedup"

        results = []

        def worker():
            results.append(_get_shared_ensemble([], _Ctx()))

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        assert compute_started.wait(timeout=2.0), "T1 never started"
        # T2 will block on the per-key lock until T1 releases
        t2.start()
        # Release T1
        compute_release.set()
        t1.join(timeout=5.0)
        t2.join(timeout=5.0)

        assert call_count["n"] == 1, (
            f"Same-key dedup failed — run_ensemble called {call_count['n']} "
            "times; expected 1. The double-check inside the per-key lock "
            "must catch the second caller after it acquires."
        )
        assert len(results) == 2
        assert results[0] == results[1], (
            "Both threads must end up with the same cached result."
        )
