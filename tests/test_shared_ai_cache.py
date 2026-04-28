"""Guardrails for `shared_ai_cache.py` and the `trade_pipeline`
integration that uses it (Lever 1 of COST_AND_QUALITY_LEVERS_PLAN.md).

Persistent SQLite cache for cross-profile AI results. Survives
process restarts so deploy-heavy days don't burn tokens on
re-fetches of values that were valid until the next cycle.

These tests prove:

1. Round-trip: put → get returns the same value (within bucket).
2. Bucket expiry: values stored in a previous bucket return None.
3. Pickle corruption returns None (graceful degradation).
4. clear_kind() evicts only the named kind, leaves others intact.
5. SQL atomicity: rapid put-then-put-then-get returns latest value.
6. trade_pipeline._get_shared_ensemble integration: when SQLite
   has a row for the current bucket, NO live API call fires.
7. trade_pipeline._get_shared_political_context integration: same
   restart-safety behavior.
8. Source-level: both functions reference shared_ai_cache.get/put.
"""

from __future__ import annotations

import importlib
import inspect
import os
import pickle
import sqlite3
import tempfile
import time
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def fresh_cache_db(monkeypatch):
    """Per-test SQLite db so we don't leak rows across tests."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    # Reload the module so its module-level _schema_initialized set
    # doesn't carry over.
    import shared_ai_cache as sac
    importlib.reload(sac)
    sac._schema_initialized.clear()
    sac.MASTER_DB = path
    yield path
    try:
        os.unlink(path)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Pure helper round-trip tests
# ---------------------------------------------------------------------------

def test_put_get_round_trip(fresh_cache_db):
    import shared_ai_cache as sac
    val = {"verdict": "BUY", "confidence": 78, "specialists": ["earn", "patt"]}
    sac.put("ensemble", "midcap", val, db_path=fresh_cache_db)
    got = sac.get("ensemble", "midcap", db_path=fresh_cache_db)
    assert got == val


def test_get_returns_none_for_missing_key(fresh_cache_db):
    import shared_ai_cache as sac
    assert sac.get("ensemble", "no_such_key", db_path=fresh_cache_db) is None


def test_get_returns_none_when_bucket_changes(fresh_cache_db):
    """Manually backdate a row to a previous bucket; get should
    treat it as a miss."""
    import shared_ai_cache as sac
    sac._init_schema(fresh_cache_db)
    conn = sqlite3.connect(fresh_cache_db)
    payload = pickle.dumps({"v": 1})
    # Bucket from 1 hour ago — guaranteed expired
    old_bucket = int((time.time() - 3600) / 1800)
    conn.execute(
        "INSERT INTO shared_ai_cache "
        "(cache_key, cache_kind, bucket, payload) VALUES (?, ?, ?, ?)",
        ("midcap", "ensemble", old_bucket, payload),
    )
    conn.commit()
    conn.close()
    assert sac.get("ensemble", "midcap", db_path=fresh_cache_db) is None


def test_get_returns_none_on_pickle_corruption(fresh_cache_db):
    """Garbage in the payload column shouldn't crash — caller treats
    as a miss and refetches."""
    import shared_ai_cache as sac
    sac._init_schema(fresh_cache_db)
    conn = sqlite3.connect(fresh_cache_db)
    cur_bucket = int(time.time() / 1800)
    conn.execute(
        "INSERT INTO shared_ai_cache "
        "(cache_key, cache_kind, bucket, payload) VALUES (?, ?, ?, ?)",
        ("midcap", "ensemble", cur_bucket, b"not-a-pickle-blob"),
    )
    conn.commit()
    conn.close()
    assert sac.get("ensemble", "midcap", db_path=fresh_cache_db) is None


def test_clear_kind_evicts_only_matching_rows(fresh_cache_db):
    import shared_ai_cache as sac
    sac.put("ensemble", "midcap", {"v": 1}, db_path=fresh_cache_db)
    sac.put("political", "global", {"v": 2}, db_path=fresh_cache_db)
    sac.clear_kind("ensemble", db_path=fresh_cache_db)
    assert sac.get("ensemble", "midcap", db_path=fresh_cache_db) is None
    assert sac.get("political", "global", db_path=fresh_cache_db) == {"v": 2}


def test_concurrent_put_atomic_replace(fresh_cache_db):
    """INSERT OR REPLACE — second write replaces first cleanly."""
    import shared_ai_cache as sac
    sac.put("ensemble", "smallcap", {"v": "first"}, db_path=fresh_cache_db)
    sac.put("ensemble", "smallcap", {"v": "second"}, db_path=fresh_cache_db)
    assert sac.get("ensemble", "smallcap", db_path=fresh_cache_db) == {"v": "second"}


# ---------------------------------------------------------------------------
# trade_pipeline integration — restart safety
# ---------------------------------------------------------------------------

def test_get_shared_ensemble_uses_persisted_cache_after_restart(fresh_cache_db, monkeypatch):
    """Simulate scheduler restart: put a value into the SQLite
    cache, clear the in-memory cache (as a process restart would),
    then call _get_shared_ensemble. It must return the persisted
    value WITHOUT calling run_ensemble."""
    import shared_ai_cache as sac
    import trade_pipeline as tp

    # Point trade_pipeline at our test DB by patching the module's
    # internal cache helpers to use it.
    monkeypatch.setattr(sac, "MASTER_DB", fresh_cache_db)

    # Pre-seed the SQLite cache as if we ran the ensemble before
    # the restart.
    persisted_result = {
        "per_symbol": {"AAPL": {"verdict": "BUY", "confidence": 80}},
        "raw": {},
        "cost_calls": 4,
    }
    sac.put("ensemble", "midcap", persisted_result, db_path=fresh_cache_db)

    # Simulate restart: clear the in-process cache.
    tp._ensemble_cache = {}
    tp._ensemble_cache_cycle = 0

    # Build a minimal ctx
    ctx = MagicMock()
    ctx.segment = "midcap"
    ctx.ai_provider = "anthropic"
    ctx.ai_model = "claude-haiku-4-5-20251001"
    ctx.ai_api_key = "fake"

    # If _get_shared_ensemble calls run_ensemble, that's the bug.
    with patch("ensemble.run_ensemble") as mock_run:
        result = tp._get_shared_ensemble([], ctx)

    assert mock_run.call_count == 0, (
        "_get_shared_ensemble called run_ensemble even though a valid "
        "persisted cache row existed. Restart-safety is broken."
    )
    assert result["per_symbol"]["AAPL"]["confidence"] == 80


def test_get_shared_political_uses_persisted_cache_after_restart(fresh_cache_db, monkeypatch):
    import shared_ai_cache as sac
    import trade_pipeline as tp

    monkeypatch.setattr(sac, "MASTER_DB", fresh_cache_db)

    persisted = {"climate": "neutral", "score": 0.5}
    sac.put("political", "global", persisted, db_path=fresh_cache_db)

    tp._political_cache = {}
    tp._political_cache_cycle = 0

    ctx = MagicMock()

    with patch("political_sentiment.get_maga_mode_context") as mock_fetch:
        result = tp._get_shared_political_context(ctx)

    assert mock_fetch.call_count == 0, (
        "_get_shared_political_context called the fresh fetcher "
        "instead of reading the persisted cache. Restart-safety broken."
    )
    assert result == persisted


# ---------------------------------------------------------------------------
# Source-level guard
# ---------------------------------------------------------------------------

def test_pipeline_helpers_reference_shared_ai_cache():
    """Both shared-cache helpers must read AND write through
    `shared_ai_cache`. Removing either side defeats restart-safety."""
    import trade_pipeline as tp
    src_ensemble = inspect.getsource(tp._get_shared_ensemble)
    src_political = inspect.getsource(tp._get_shared_political_context)
    for label, src in [("_get_shared_ensemble", src_ensemble),
                        ("_get_shared_political_context", src_political)]:
        assert "shared_ai_cache" in src, (
            f"REGRESSION: {label} no longer references shared_ai_cache. "
            f"Restart-safety regressed; deploys will burn fresh API "
            f"calls. See COST_AND_QUALITY_LEVERS_PLAN.md Lever 1."
        )
