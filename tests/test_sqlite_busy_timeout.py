"""Pin SQLite WAL + busy_timeout configuration on every connection.

Caught 2026-05-09 (Issue 9 root-cause analysis): every `_get_conn`
helper in models.py / journal.py / ai_tracker.py / self_tuning.py
set `PRAGMA journal_mode=WAL` but did NOT set `PRAGMA busy_timeout`.
SQLite's default busy_timeout is 0 — any contested write lock
raises `OperationalError: database is locked` instantly. WAL alone
doesn't help when both sides race for the same write lock; only
busy_timeout + WAL together make concurrent reader/writer work.

The result was a real but rarely-firing failure mode that the
silent-pass swallows in views.py were protecting against. With
busy_timeout=5000 (5 second wait), the failure can no longer occur
under any normal write/read race — making the swallows themselves
unnecessary.

This test:
1. Pins busy_timeout is set (and is at least 1000ms) on every helper.
2. Behavioral: holds a write lock briefly while another connection
   reads from the same DB; assert the read succeeds (would throw
   `database is locked` before this fix).
3. Cross-cutting AST guardrail: any new `_get_conn`-style helper in
   the codebase must set both PRAGMA journal_mode=WAL AND PRAGMA
   busy_timeout. Future regressions fail the test.
"""

import ast
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


# ---------------------------------------------------------------------------
# Layer 1 — pragmas are actually set
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_name", [
    "models", "journal", "ai_tracker", "self_tuning",
])
def test_get_conn_sets_busy_timeout(tmp_path, monkeypatch, module_name):
    """Each helper's _get_conn must set busy_timeout >= 1000ms."""
    import importlib
    import config
    db_file = str(tmp_path / "tt.db")
    monkeypatch.setattr(config, "DB_PATH", db_file)

    mod = importlib.import_module(module_name)
    conn = mod._get_conn(db_file)
    try:
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()

    assert timeout >= 1000, (
        f"{module_name}._get_conn set busy_timeout={timeout}ms — "
        "must be >= 1000 to ride out scheduler-write races. The "
        "silent-pass swallows in views.py exist to hide failures "
        "this PRAGMA prevents."
    )
    assert journal.lower() == "wal", (
        f"{module_name}._get_conn set journal_mode={journal}, must be wal."
    )


# ---------------------------------------------------------------------------
# Layer 2 — behavioral: concurrent read+write actually works
# ---------------------------------------------------------------------------


def test_concurrent_read_during_write_succeeds(tmp_path):
    """Hold a write transaction briefly on connection A while
    connection B reads from the same DB. Pre-fix this raises
    OperationalError instantly; post-fix it succeeds because
    busy_timeout makes the read wait briefly."""
    from models import open_profile_db

    db_file = str(tmp_path / "concur.db")

    # Initialize the table so we have something to read
    init_conn = open_profile_db(db_file)
    init_conn.execute(
        "CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, val TEXT)"
    )
    init_conn.execute("INSERT INTO t (val) VALUES ('seed')")
    init_conn.commit()
    init_conn.close()

    write_started = threading.Event()
    write_done = threading.Event()

    def writer():
        c = open_profile_db(db_file)
        try:
            c.execute("BEGIN IMMEDIATE")
            c.execute("INSERT INTO t (val) VALUES ('mid-write')")
            write_started.set()
            # Hold the write lock for ~500ms
            time.sleep(0.5)
            c.execute("COMMIT")
        finally:
            c.close()
        write_done.set()

    t = threading.Thread(target=writer)
    t.start()
    write_started.wait(timeout=2.0)
    assert write_started.is_set(), "writer never started"

    # Now read while the writer holds the lock — should succeed via
    # busy_timeout (waits up to 5s for the lock to clear).
    reader = open_profile_db(db_file)
    try:
        rows = reader.execute("SELECT COUNT(*) FROM t").fetchone()
    finally:
        reader.close()

    assert rows[0] >= 1
    t.join(timeout=2.0)
    assert write_done.is_set()


# ---------------------------------------------------------------------------
# Layer 3 — open_profile_db gives the dashboard schema-migrated, locked-
#            up-readable per-profile connections
# ---------------------------------------------------------------------------


def test_open_profile_db_creates_ai_predictions_table_if_missing(tmp_path):
    """A brand-new profile DB (no writes ever) must still have
    ai_predictions queryable when the dashboard reads it. Pre-fix
    the dashboard would error out and the swallow would hide it."""
    from models import open_profile_db
    db_file = str(tmp_path / "fresh.db")
    assert not Path(db_file).exists()

    conn = open_profile_db(db_file)
    try:
        # If init_tracker_db wasn't called, this raises
        # "no such table: ai_predictions"
        rows = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions"
        ).fetchall()
    finally:
        conn.close()
    assert rows[0][0] == 0


def test_open_profile_db_sets_busy_timeout(tmp_path):
    from models import open_profile_db
    db_file = str(tmp_path / "bt.db")
    conn = open_profile_db(db_file)
    try:
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    finally:
        conn.close()
    assert timeout >= 1000


# ---------------------------------------------------------------------------
# Layer 4 — AST guardrail: every _get_conn helper must set both PRAGMAs
# ---------------------------------------------------------------------------


HELPER_FILES = ["models.py", "journal.py", "ai_tracker.py", "self_tuning.py"]


def test_every_get_conn_helper_sets_wal_and_busy_timeout():
    """Any function literally named `_get_conn` in our connection-
    helper modules must set both PRAGMA journal_mode=WAL and PRAGMA
    busy_timeout. Catches regressions where someone adds a new
    connection helper without these PRAGMAs."""
    repo_root = os.path.join(os.path.dirname(__file__), os.pardir)
    for fname in HELPER_FILES:
        path = os.path.join(repo_root, fname)
        with open(path) as f:
            src = f.read()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name != "_get_conn":
                continue
            # Concat every string literal in the body
            literals = []
            for n in ast.walk(node):
                if isinstance(n, ast.Constant) and isinstance(n.value, str):
                    literals.append(n.value.lower())
            blob = " ".join(literals)
            assert "journal_mode=wal" in blob, (
                f"{fname}::_get_conn does not set "
                "PRAGMA journal_mode=WAL"
            )
            assert "busy_timeout" in blob, (
                f"{fname}::_get_conn does not set "
                "PRAGMA busy_timeout. Concurrent scheduler writes "
                "will cause dashboard reads to error out instantly. "
                "Add: conn.execute('PRAGMA busy_timeout=5000')"
            )
