"""Structural guardrail: every DB-init function must be safely
runnable multiple times against the same DB without breaking the
schema.

The bug class.
init_db / init_user_db are called in many places — startup, lazy
init on first profile-cycle, manual scripts, tests. If any
ALTER TABLE statement isn't wrapped in a `try: ... except
sqlite3.OperationalError: pass` (the existing pattern), then re-
running the function on a DB that already has the column fails
with "duplicate column name X".

The bug shape: deploy ships a new column. Some profiles get the
column on first cycle (fresh init). Other profiles already had a
fresh DB at deploy time — calling init AGAIN fails because the
column exists. Result: lazy-init breaks for those profiles. Fix
becomes a manual loop (which is exactly what I had to do today
for ai_predictions.data_quality).

This test enforces idempotence end-to-end: instantiate fresh DB,
run init, run init AGAIN, assert (a) no exception, (b) schema
identical between runs. Catches non-idempotent migrations BEFORE
they hit production.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


def _table_schemas(db_path: str) -> dict:
    """Return {table_name: [(col_name, col_type), ...]} for every
    user-defined table in the DB."""
    conn = sqlite3.connect(db_path)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()]
        out = {}
        for t in tables:
            cols = [(r[1], r[2]) for r in conn.execute(
                f"PRAGMA table_info({t})"
            ).fetchall()]
            out[t] = sorted(cols)
        return out
    finally:
        conn.close()


class TestJournalInitDbIdempotent:
    def test_init_db_runs_twice_safely(self, tmp_path):
        """journal.init_db on per-profile DB. Run, snapshot schema,
        run again, snapshot, compare."""
        db = str(tmp_path / "p.db")
        from journal import init_db
        # First init — fresh DB
        init_db(db)
        first = _table_schemas(db)
        # Second init — should be a no-op for the schema
        init_db(db)
        second = _table_schemas(db)
        assert first == second, (
            "journal.init_db is not idempotent. Schema differs "
            "between first and second call.\n\n"
            "Diff (first → second):\n"
            + _format_schema_diff(first, second)
            + "\n\nFix: wrap any `ALTER TABLE ... ADD COLUMN` in "
            "`try: ... except sqlite3.OperationalError: pass`. "
            "The duplicate-column error is benign on re-init."
        )

    def test_init_db_doesnt_error_on_pre_existing_db(self, tmp_path):
        """Concrete shape: simulate a deploy that adds a new column.
        First create a DB at the OLD schema, then call init_db (which
        carries the new schema). Must succeed, not error."""
        db = str(tmp_path / "old.db")
        # Create a minimal trades table with old schema
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE trades (id INTEGER PRIMARY KEY, "
            "timestamp TEXT, symbol TEXT, side TEXT, qty REAL)"
        )
        conn.commit()
        conn.close()
        # Now run init_db — it will try to ALTER TABLE many times
        # adding the columns the new schema expects. All adds must
        # succeed-or-skip; none may raise.
        from journal import init_db
        init_db(db)  # if this raises, test fails


class TestModelsInitUserDbIdempotent:
    def test_init_user_db_runs_twice_safely(self, tmp_path, monkeypatch):
        """models.init_user_db on master DB. Run, snapshot, run, snapshot,
        compare. Migrations that aren't idempotent (e.g., a new schema-
        flip migration without a marker check) would surface here."""
        import config
        import models
        db = str(tmp_path / "master.db")
        monkeypatch.setattr(config, "DB_PATH", db)
        models.init_user_db()
        first = _table_schemas(db)
        models.init_user_db()
        second = _table_schemas(db)
        assert first == second, (
            "models.init_user_db is not idempotent.\n\n"
            "Diff:\n" + _format_schema_diff(first, second)
        )


def _format_schema_diff(a: dict, b: dict) -> str:
    out = []
    all_tables = sorted(set(a.keys()) | set(b.keys()))
    for t in all_tables:
        if t not in a:
            out.append(f"  + table {t} appeared on second init")
            continue
        if t not in b:
            out.append(f"  - table {t} disappeared on second init")
            continue
        a_cols = set(a[t])
        b_cols = set(b[t])
        added = b_cols - a_cols
        removed = a_cols - b_cols
        if added:
            out.append(f"  table {t}: +cols {sorted(added)}")
        if removed:
            out.append(f"  table {t}: -cols {sorted(removed)}")
    return "\n".join(out) if out else "  (no diff but assertion fired — investigate)"
