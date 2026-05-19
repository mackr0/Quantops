"""db_integrity._all_db_paths — phantom-profile filter.

2026-05-19 incident: `quantopsai_profile_25.db` existed on disk as
a 0-byte shell with no corresponding row in `trading_profiles`.
The integrity gate enumerated by glob, flagged it as critical
corruption, and halted the scheduler. 19 restart loops in 25
minutes; all 13 real profiles (ids 12-24) stalled.

Fix: `_all_db_paths` now reads master.trading_profiles to enumerate
KNOWN profile ids. Profile journal files whose id isn't in that
set are treated as phantoms — logged loudly but excluded from the
integrity scan so they can't halt startup.

Tests pin:
- A phantom file (no master row) is skipped.
- A real profile file (has master row) is still scanned.
- Missing/corrupt master falls back to legacy (include everything).
- Malformed filename is included defensively.
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from db_integrity import _all_db_paths, _known_profile_ids


def _make_master_db(path, profile_ids):
    """Create a minimal master DB with the given profile ids."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE trading_profiles ("
        "id INTEGER PRIMARY KEY, name TEXT, enabled INTEGER)"
    )
    for pid in profile_ids:
        conn.execute(
            "INSERT INTO trading_profiles (id, name, enabled) "
            "VALUES (?, ?, 1)", (pid, f"profile_{pid}"),
        )
    conn.commit()
    conn.close()


def _touch_profile_file(repo_root, pid, content=b""):
    """Create a profile_<pid>.db file with optional content."""
    p = os.path.join(repo_root, f"quantopsai_profile_{pid}.db")
    with open(p, "wb") as f:
        f.write(content)
    return p


class TestKnownProfileIds:
    def test_reads_ids_from_trading_profiles(self, tmp_path):
        master = tmp_path / "quantopsai.db"
        _make_master_db(master, [12, 13, 14])
        ids = _known_profile_ids(str(master))
        assert ids == {12, 13, 14}

    def test_returns_none_when_master_missing(self, tmp_path):
        ids = _known_profile_ids(str(tmp_path / "nonexistent.db"))
        assert ids is None

    def test_returns_none_when_master_corrupt(self, tmp_path):
        master = tmp_path / "quantopsai.db"
        master.write_bytes(b"not a sqlite file at all")
        ids = _known_profile_ids(str(master))
        assert ids is None

    def test_returns_none_when_table_missing(self, tmp_path):
        master = tmp_path / "quantopsai.db"
        conn = sqlite3.connect(str(master))
        conn.execute("CREATE TABLE something_else (x)")
        conn.commit()
        conn.close()
        ids = _known_profile_ids(str(master))
        assert ids is None


class TestAllDbPathsFilteringPhantoms:
    """The 2026-05-19 regression class. Phantom profile files must
    not appear in the integrity-scan path list, but real ones must."""

    def test_phantom_profile_db_is_excluded(self, tmp_path):
        """The exact 2026-05-19 scenario: master has profiles 12-24,
        but profile_25.db exists on disk as a 0-byte phantom. The
        phantom must NOT appear in the scan list."""
        master = tmp_path / "quantopsai.db"
        _make_master_db(master, list(range(12, 25)))   # ids 12-24
        # Touch a real profile and a phantom
        real = _touch_profile_file(str(tmp_path), 12)
        phantom = _touch_profile_file(str(tmp_path), 25)  # 0 bytes

        paths = _all_db_paths(repo_root=str(tmp_path))
        assert real in paths, "real profile file must be scanned"
        assert phantom not in paths, (
            "phantom profile file (id=25, no master row) must be "
            "excluded — this is the 2026-05-19 regression check"
        )

    def test_all_real_profiles_included(self, tmp_path):
        master = tmp_path / "quantopsai.db"
        _make_master_db(master, [12, 13, 14])
        files = [
            _touch_profile_file(str(tmp_path), pid)
            for pid in (12, 13, 14)
        ]
        paths = _all_db_paths(repo_root=str(tmp_path))
        for f in files:
            assert f in paths

    def test_master_unreadable_falls_back_to_include_everything(
        self, tmp_path,
    ):
        """If master can't be read, we don't have a known-id set to
        filter against — better to include every profile_*.db and
        let the integrity check halt on real corruption than to
        silently skip a real profile."""
        master = tmp_path / "quantopsai.db"
        master.write_bytes(b"garbage")  # unreadable
        files = [
            _touch_profile_file(str(tmp_path), pid)
            for pid in (12, 25, 99)
        ]
        paths = _all_db_paths(repo_root=str(tmp_path))
        # Every profile file present must be in the list — no filter
        for f in files:
            assert f in paths

    def test_master_db_itself_always_included(self, tmp_path):
        master = tmp_path / "quantopsai.db"
        _make_master_db(master, [12])
        paths = _all_db_paths(repo_root=str(tmp_path))
        assert str(master) in paths

    def test_malformed_filename_included_defensively(self, tmp_path):
        """A profile filename that doesn't match the standard pattern
        is included anyway — we don't want a typo to silently skip
        a file from the integrity scan."""
        master = tmp_path / "quantopsai.db"
        _make_master_db(master, [12])
        # Standard pattern is quantopsai_profile_<digits>.db
        weird = tmp_path / "quantopsai_profile_abc.db"
        weird.write_bytes(b"")
        paths = _all_db_paths(repo_root=str(tmp_path))
        assert str(weird) in paths


class TestEndToEndPhantomDoesNotHaltScheduler:
    """Structural test — exercises the full scan path the scheduler
    runs on startup. With my fix, a phantom file produces a clean
    scan (no `corrupt` results); without it, the scan would have
    one entry with `status='corrupt'`."""

    def test_phantom_produces_no_corrupt_result_in_scan(self, tmp_path):
        from db_integrity import check_all_dbs, critical_corrupt
        master = tmp_path / "quantopsai.db"
        _make_master_db(master, [12])
        _touch_profile_file(str(tmp_path), 12, content=_valid_sqlite())
        _touch_profile_file(str(tmp_path), 99)  # phantom: 0 bytes

        results = check_all_dbs(repo_root=str(tmp_path))
        crit = critical_corrupt(results)
        assert "quantopsai_profile_99.db" not in [
            os.path.basename(p) for p in crit
        ], (
            "phantom DB must not appear in critical_corrupt — the "
            "2026-05-19 incident was exactly this list halting "
            "the scheduler"
        )


def _valid_sqlite() -> bytes:
    """Bytes of a minimal valid SQLite DB so the integrity check
    on the 'real' profile passes."""
    import io, tempfile
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as t:
        p = t.name
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE x (y INTEGER)")
    conn.commit()
    conn.close()
    with open(p, "rb") as f:
        body = f.read()
    os.unlink(p)
    return body
