"""SQLite-backed shared cache for cross-profile AI results that
should survive process restarts.

Lever 1 of `COST_AND_QUALITY_LEVERS_PLAN.md`. Replaces the
module-level dicts in `trade_pipeline.py` (`_ensemble_cache`,
`_political_cache`) so that a scheduler restart doesn't force a
fresh API call when a valid cached value is just minutes old.

The day this was written (2026-04-27) had 16 deploys. Each deploy
restarted the scheduler → wiped the in-memory cache → forced fresh
ensemble fires (~$0.50 of today's elevated AI cost). On normal
deploy days the savings are smaller but the structural protection
is permanent.

Schema (in master `quantopsai.db`):

    CREATE TABLE shared_ai_cache (
        cache_key   TEXT NOT NULL,
        cache_kind  TEXT NOT NULL,   -- 'ensemble' | 'political' | ...
        bucket      INTEGER NOT NULL,-- int(time/<TTL>)
        payload     BLOB NOT NULL,   -- pickle-serialized value
        fetched_at  TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (cache_key, cache_kind)
    );

Read path: caller passes `cache_kind`, `cache_key`, `bucket_seconds`.
We compute the current bucket; if a row exists with matching
`(cache_kind, cache_key, bucket)`, we return its unpickled payload.
Otherwise the caller runs the fresh API call and writes back.

Write path: `INSERT OR REPLACE` is atomic — concurrent processes
that both miss the cache will both run the API call (one extra
fire), but the second write replaces the first cleanly.

Pickle failures (e.g., schema drift) are silently treated as cache
misses so the live system never crashes on a stale binary blob.
"""

from __future__ import annotations

import logging
import os
import pickle
import sqlite3
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

MASTER_DB = os.environ.get("QUANTOPSAI_MASTER_DB", "quantopsai.db")

_schema_lock = threading.Lock()
_schema_initialized: set = set()


def _resolve_db(db_path: Optional[str]) -> str:
    """Read MASTER_DB at call time (not import time) so tests that
    monkey-patch the module-level constant take effect."""
    if db_path:
        return db_path
    # Module-level lookup — picks up monkey-patches.
    import sys
    mod = sys.modules.get(__name__)
    if mod is not None:
        return getattr(mod, "MASTER_DB", "quantopsai.db")
    return MASTER_DB


def _init_schema(db_path: Optional[str] = None) -> None:
    db_path = _resolve_db(db_path)
    if not db_path:
        return
    with _schema_lock:
        if db_path in _schema_initialized:
            return
        try:
            conn = sqlite3.connect(db_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS shared_ai_cache (
                    cache_key   TEXT NOT NULL,
                    cache_kind  TEXT NOT NULL,
                    bucket      INTEGER NOT NULL,
                    payload     BLOB NOT NULL,
                    fetched_at  TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (cache_key, cache_kind)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_shared_ai_cache_kind "
                "ON shared_ai_cache(cache_kind, bucket)"
            )
            conn.commit()
            conn.close()
            _schema_initialized.add(db_path)
        except Exception as exc:
            logger.warning("Failed to init shared_ai_cache: %s", exc)


def get(cache_kind: str, cache_key: str,
        bucket_seconds: int = 1800,
        db_path: Optional[str] = None) -> Optional[Any]:
    """Return the cached value for `(cache_kind, cache_key)` if it
    exists for the current `bucket_seconds` window.

    Returns None on cache miss, schema absent, pickle corruption,
    or any DB error — caller should treat None as "fetch fresh."
    """
    db_path = _resolve_db(db_path)
    if not db_path or not cache_kind or not cache_key:
        return None
    _init_schema(db_path)
    cur_bucket = int(time.time() / bucket_seconds)
    try:
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT payload, bucket FROM shared_ai_cache "
            "WHERE cache_kind = ? AND cache_key = ?",
            (cache_kind, cache_key),
        ).fetchone()
        conn.close()
    except Exception:
        return None
    if not row:
        return None
    payload, stored_bucket = row[0], row[1]
    if stored_bucket != cur_bucket:
        return None
    try:
        return pickle.loads(payload)
    except Exception:
        # Schema drift / corruption — treat as miss; caller refetches.
        return None


def put(cache_kind: str, cache_key: str, value: Any,
        bucket_seconds: int = 1800,
        db_path: Optional[str] = None) -> None:
    """Persist `value` under `(cache_kind, cache_key)` for the
    current bucket. Atomic via INSERT OR REPLACE — concurrent writers
    don't corrupt the row.

    Pickle errors are swallowed (log only) so a value that can't be
    serialized doesn't crash the caller.
    """
    db_path = _resolve_db(db_path)
    if not db_path or not cache_kind or not cache_key:
        return
    _init_schema(db_path)
    cur_bucket = int(time.time() / bucket_seconds)
    try:
        payload = pickle.dumps(value)
    except Exception as exc:
        logger.warning("Failed to pickle cache value for %s/%s: %s",
                       cache_kind, cache_key, exc)
        return
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT OR REPLACE INTO shared_ai_cache "
            "(cache_key, cache_kind, bucket, payload, fetched_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            (cache_key, cache_kind, cur_bucket, payload),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning("Failed to write shared_ai_cache for %s/%s: %s",
                       cache_kind, cache_key, exc)


def clear_kind(cache_kind: str, db_path: Optional[str] = None) -> None:
    """Manual eviction. Used by tests + future ops tooling."""
    db_path = _resolve_db(db_path)
    if not db_path or not cache_kind:
        return
    _init_schema(db_path)
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            "DELETE FROM shared_ai_cache WHERE cache_kind = ?",
            (cache_kind,),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass
