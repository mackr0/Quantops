"""Profile create/delete must never leave 0-byte phantom journals.

2026-05-19 outage root cause: a 0-byte `quantopsai_profile_25.db`
caused the integrity gate to halt the scheduler in a 19-restart
loop. Forensic likely-cause: a `sqlite3.connect()` call created the
file but no transaction had committed the SQLite header before the
process was SIGKILLed.

Two complementary fixes pin the issue closed:

1. `create_trading_profile` eagerly initialises the journal DB by
   calling `open_profile_db()` immediately after the master INSERT
   commits. This forces a schema write, so the file either has a
   valid SQLite header from creation or doesn't exist at all —
   never the 0-byte limbo.

2. `delete_trading_profile` renames the journal file to
   `<path>.deleted-<utc-iso>` after the master DELETE so the
   orphan stops matching the `quantopsai_profile_*.db` glob the
   integrity gate enumerates.

These tests pin both contracts structurally so future refactors
can't regress.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture
def isolated_cwd(tmp_path, monkeypatch):
    """Run each test in its own tmp dir with config.DB_PATH pointed
    at a fresh master DB. Profile journal files (created via the
    relative path `quantopsai_profile_<N>.db`) land in the same
    tmp dir so we can inspect them without colliding with the real
    /opt/quantopsai installation."""
    monkeypatch.chdir(tmp_path)
    # Stage a master DB with the schema create_trading_profile expects
    master_path = str(tmp_path / "quantopsai.db")
    import config
    monkeypatch.setattr(config, "DB_PATH", master_path)
    # Init the master schema — init_user_db creates both users and
    # trading_profiles tables in one executescript.
    from models import init_user_db
    init_user_db(master_path)
    # Create a user row so create_trading_profile's FK is satisfied
    conn = sqlite3.connect(master_path)
    conn.execute(
        "INSERT INTO users (email, password_hash) VALUES (?, ?)",
        ("test@example.com", "x"),
    )
    conn.commit()
    user_id = conn.execute("SELECT id FROM users").fetchone()[0]
    conn.close()
    return {"tmp_path": tmp_path, "master_path": master_path,
            "user_id": user_id}


# ---------------------------------------------------------------------------
# Create-path: journal file must have a valid SQLite header before return
# ---------------------------------------------------------------------------

class TestCreateInitialisesJournalEagerly:
    def test_journal_file_exists_after_create(self, isolated_cwd):
        from models import create_trading_profile
        pid = create_trading_profile(
            isolated_cwd["user_id"], "TestProfile", "largecap",
        )
        journal = isolated_cwd["tmp_path"] / f"quantopsai_profile_{pid}.db"
        assert journal.exists(), (
            "Journal file must exist after create_trading_profile "
            "returns (eager init contract)"
        )

    def test_journal_file_is_not_zero_bytes(self, isolated_cwd):
        """The 2026-05-19 regression class: a 0-byte SQLite file
        passes `sqlite3.connect` but fails `db_integrity.check_db`
        because the SQLite header is missing."""
        from models import create_trading_profile
        pid = create_trading_profile(
            isolated_cwd["user_id"], "TestProfile", "largecap",
        )
        journal = isolated_cwd["tmp_path"] / f"quantopsai_profile_{pid}.db"
        size = journal.stat().st_size
        assert size > 0, (
            f"Journal file is {size} bytes — would be flagged as "
            f"phantom by db_integrity. The 2026-05-19 outage was "
            f"caused by exactly this state."
        )

    def test_journal_passes_db_integrity_check(self, isolated_cwd):
        """End-to-end: the freshly-created journal must pass the
        same integrity check that halts the scheduler on startup."""
        from models import create_trading_profile
        from db_integrity import check_db
        pid = create_trading_profile(
            isolated_cwd["user_id"], "TestProfile", "largecap",
        )
        journal = str(isolated_cwd["tmp_path"]
                       / f"quantopsai_profile_{pid}.db")
        result = check_db(journal)
        assert result["status"] == "ok", (
            f"db_integrity.check_db returned {result} on the "
            f"freshly-created journal — the integrity gate would "
            f"halt the scheduler on next startup"
        )

    def test_journal_has_trades_table(self, isolated_cwd):
        """Eager init must produce the canonical schema, not just a
        valid-header empty file. Otherwise the first trade write
        would still need to migrate."""
        from models import create_trading_profile
        pid = create_trading_profile(
            isolated_cwd["user_id"], "TestProfile", "largecap",
        )
        journal = str(isolated_cwd["tmp_path"]
                       / f"quantopsai_profile_{pid}.db")
        conn = sqlite3.connect(journal)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        assert "trades" in tables, (
            f"journal must contain canonical trades table; "
            f"got {tables}"
        )


# ---------------------------------------------------------------------------
# Delete-path: journal file must be renamed aside, not left as orphan
# ---------------------------------------------------------------------------

class TestDeleteRenamesJournalAside:
    def test_delete_renames_journal_with_deleted_suffix(self, isolated_cwd):
        from models import create_trading_profile, delete_trading_profile
        pid = create_trading_profile(
            isolated_cwd["user_id"], "TestProfile", "largecap",
        )
        original = isolated_cwd["tmp_path"] / f"quantopsai_profile_{pid}.db"
        assert original.exists()

        delete_trading_profile(pid)

        # Original path must be gone
        assert not original.exists(), (
            "Original journal path must be renamed away on delete"
        )
        # A `.deleted-<ts>` sibling must exist
        siblings = list(isolated_cwd["tmp_path"].glob(
            f"quantopsai_profile_{pid}.db.deleted-*"
        ))
        assert len(siblings) == 1, (
            f"Expected exactly one .deleted-<ts> rename; got "
            f"{[s.name for s in siblings]}"
        )

    def test_renamed_journal_is_not_a_phantom_glob_match(self, isolated_cwd):
        """The integrity-gate glob `quantopsai_profile_*.db` must
        not pick up the renamed-aside file. This is the actual
        prevention that makes deletes safe."""
        from models import create_trading_profile, delete_trading_profile
        pid = create_trading_profile(
            isolated_cwd["user_id"], "TestProfile", "largecap",
        )
        delete_trading_profile(pid)

        import glob as _glob
        matches = _glob.glob(str(
            isolated_cwd["tmp_path"] / "quantopsai_profile_*.db"
        ))
        # Anything matching would be a journal file the integrity
        # gate would scan. The renamed `.deleted-<ts>` file must NOT
        # match because its name doesn't end in `.db`.
        assert all("deleted" not in os.path.basename(m) for m in matches), (
            f"Renamed-aside files must not match the journal glob; "
            f"got {matches}"
        )

    def test_delete_master_row_succeeds_even_if_rename_fails(
        self, isolated_cwd, monkeypatch,
    ):
        """The master DELETE is the canonical action; the rename is
        best-effort. A filesystem permission glitch on rename must
        not block the master DELETE."""
        from models import create_trading_profile, delete_trading_profile
        pid = create_trading_profile(
            isolated_cwd["user_id"], "TestProfile", "largecap",
        )

        # Force os.rename to raise so we exercise the failure path
        import os as _os
        original_rename = _os.rename
        def boom(*a, **k):
            raise OSError("simulated filesystem permission error")
        monkeypatch.setattr(_os, "rename", boom)

        # Must NOT raise — master DELETE is the load-bearing action
        delete_trading_profile(pid)

        # Restore rename so cleanup works
        monkeypatch.setattr(_os, "rename", original_rename)

        # Verify the master DELETE actually happened
        conn = sqlite3.connect(isolated_cwd["master_path"])
        row = conn.execute(
            "SELECT id FROM trading_profiles WHERE id=?", (pid,),
        ).fetchone()
        conn.close()
        assert row is None, (
            "Master DELETE must succeed even when journal rename fails"
        )

    def test_delete_when_journal_file_does_not_exist(self, isolated_cwd):
        """If the journal was never created (or was already cleaned
        up manually), delete_trading_profile must still succeed."""
        from models import create_trading_profile, delete_trading_profile
        pid = create_trading_profile(
            isolated_cwd["user_id"], "TestProfile", "largecap",
        )
        # Manually remove the journal first
        journal = isolated_cwd["tmp_path"] / f"quantopsai_profile_{pid}.db"
        journal.unlink()

        # Must not raise
        delete_trading_profile(pid)

        # Master row gone
        conn = sqlite3.connect(isolated_cwd["master_path"])
        row = conn.execute(
            "SELECT id FROM trading_profiles WHERE id=?", (pid,),
        ).fetchone()
        conn.close()
        assert row is None


# ---------------------------------------------------------------------------
# End-to-end: create → delete → integrity scan finds zero orphans
# ---------------------------------------------------------------------------

class TestEndToEndLifecycleLeavesNoPhantom:
    def test_create_then_delete_passes_phantom_filter(self, isolated_cwd):
        """The full lifecycle the 2026-05-19 incident exposed.
        After create + delete, db_integrity's _all_db_paths must
        return zero profile_*.db files — no orphan to mistake for
        a phantom on the next scheduler startup."""
        from models import create_trading_profile, delete_trading_profile
        from db_integrity import _all_db_paths
        pid = create_trading_profile(
            isolated_cwd["user_id"], "TestProfile", "largecap",
        )
        delete_trading_profile(pid)

        paths = _all_db_paths(repo_root=str(isolated_cwd["tmp_path"]))
        profile_paths = [
            p for p in paths
            if "quantopsai_profile_" in os.path.basename(p)
            and not os.path.basename(p).endswith(
                tuple(f"deleted-{i}" for i in "0123456789")
            )
        ]
        # Even more strictly: the integrity scan path list must
        # contain ZERO profile journal files matching the standard
        # `_<digits>.db` pattern after a clean delete.
        import re
        standard = re.compile(r"quantopsai_profile_\d+\.db$")
        leaks = [p for p in profile_paths
                  if standard.search(os.path.basename(p))]
        assert leaks == [], (
            f"After create+delete, no quantopsai_profile_<N>.db "
            f"file should remain; found {leaks}"
        )
