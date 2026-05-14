"""Structural guardrail: every per-profile DB
(`quantopsai_profile_*.db`) must have the same schema.

The bug class.
On 2026-05-13, after deploying `data_quality TEXT` to
`ai_predictions`, only 1 of 11 per-profile DBs had the column —
because `journal.init_db()` runs lazily (when a profile's pipeline
next touches its DB), not eagerly at deploy time.

The 10 profiles missing the column would have had analytics queries
silently see NULL for every row (or in some cases crash with
"no such column"). I had to manually loop through all 11 DBs
forcing init_db on each.

The bug shape is general: ANY new column added to the per-profile
schema can be missing on N-1 of N profiles between deploy and
each profile's first cycle. If the column is read by analytics in
the meantime, results are wrong.

This test spins up multiple per-profile DBs from scratch via
init_db, then asserts they all have identical schemas. Failures
mean either:
  (a) init_db is non-deterministic (writes a column on some DBs
      not others), or
  (b) some hidden code path creates a profile DB without calling
      init_db, leaving an incomplete schema.

In production, this test won't catch the deploy-time lag (since
it spins up fresh DBs that always run init_db). But it guards
against the future class where someone adds a column-creation
path that bypasses init_db.

Production-time defense lives in models.init_user_db's
post-deploy migration: see if we should iterate every profile DB
and force init_db. (Not in scope for this test; flagging as
follow-up.)
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


def _all_table_columns(db_path: str) -> dict:
    """Return {table_name: set(col_name)} for every user table."""
    conn = sqlite3.connect(db_path)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchall()]
        return {
            t: {r[1] for r in conn.execute(
                f"PRAGMA table_info({t})"
            ).fetchall()}
            for t in tables
        }
    finally:
        conn.close()


class TestPerProfileSchemaConsistency:
    def test_fresh_inits_produce_identical_schemas(self, tmp_path):
        """Spin up 5 fresh per-profile DBs, init each, assert they
        all have matching schemas. If init_db is non-deterministic
        — e.g., relies on global state, environment, or order-of-
        registration — this surfaces immediately."""
        from journal import init_db
        dbs = []
        for i in range(5):
            db = str(tmp_path / f"p{i}.db")
            init_db(db)
            dbs.append(db)
        # Snapshot each
        schemas = [_all_table_columns(db) for db in dbs]
        # Assert all match the first
        baseline = schemas[0]
        for i, s in enumerate(schemas[1:], start=1):
            assert s == baseline, (
                f"Profile {i} schema differs from profile 0 after "
                f"identical init_db calls.\n\n"
                f"Difference:\n"
                + _format_diff(baseline, s)
                + "\n\nLikely cause: init_db is non-deterministic "
                "(reads global state, env, or has order dependency)."
            )

    def test_init_db_creates_data_quality_on_ai_predictions(
            self, tmp_path):
        """Specific regression for 2026-05-13 incident: new
        `ai_predictions.data_quality` column must appear on every
        fresh init. Without this targeted check, the schema
        consistency test above could pass even if data_quality was
        consistently MISSING from all profiles."""
        from journal import init_db
        db = str(tmp_path / "p.db")
        init_db(db)
        conn = sqlite3.connect(db)
        try:
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(ai_predictions)"
            ).fetchall()}
        finally:
            conn.close()
        assert "data_quality" in cols, (
            "ai_predictions.data_quality column not created by "
            "fresh init_db. This was today's incident — column "
            "added but not on all profiles. Verify the migration "
            "list in journal.init_db includes data_quality."
        )

    def test_init_db_creates_data_quality_on_trades(self, tmp_path):
        """Mirror check for trades.data_quality (the original 2026-
        05-12 column added during the phantom-stop incident)."""
        from journal import init_db
        db = str(tmp_path / "p.db")
        init_db(db)
        conn = sqlite3.connect(db)
        try:
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(trades)"
            ).fetchall()}
        finally:
            conn.close()
        assert "data_quality" in cols


def _format_diff(a: dict, b: dict) -> str:
    out = []
    all_tables = sorted(set(a.keys()) | set(b.keys()))
    for t in all_tables:
        if t not in a:
            out.append(f"  + table {t} only in profile-N")
            continue
        if t not in b:
            out.append(f"  - table {t} only in profile-0")
            continue
        added = b[t] - a[t]
        removed = a[t] - b[t]
        if added:
            out.append(f"  {t}: profile-N has extra cols {sorted(added)}")
        if removed:
            out.append(f"  {t}: profile-N missing cols {sorted(removed)}")
    return "\n".join(out) if out else "  (no diff)"
