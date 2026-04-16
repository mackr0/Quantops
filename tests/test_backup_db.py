"""Tests for backup_db — daily SQLite snapshot + rotation."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta

import pytest


def _seed_db(path: str, rows: int = 3) -> None:
    """Create a tiny SQLite DB with one table + N rows."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (k TEXT, v INTEGER)")
    for i in range(rows):
        conn.execute("INSERT INTO t VALUES (?, ?)", (f"k{i}", i))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# backup_one
# ---------------------------------------------------------------------------

class TestBackupOne:
    def test_creates_valid_copy(self, tmp_path):
        from backup_db import backup_one
        src = tmp_path / "src.db"
        dest = tmp_path / "backup" / "src.2026-04-14.db"
        _seed_db(str(src), rows=5)

        ok = backup_one(str(src), str(dest))
        assert ok is True
        assert dest.exists()

        # Verify the copy is a valid SQLite DB with the same data
        conn = sqlite3.connect(str(dest))
        n = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        conn.close()
        assert n == 5

    def test_missing_source_returns_false(self, tmp_path):
        from backup_db import backup_one
        ok = backup_one(str(tmp_path / "missing.db"),
                        str(tmp_path / "out.db"))
        assert ok is False

    def test_creates_dest_directory(self, tmp_path):
        from backup_db import backup_one
        src = tmp_path / "s.db"
        _seed_db(str(src))
        # Multi-level dest dir doesn't exist yet
        dest = tmp_path / "a" / "b" / "c" / "out.db"
        ok = backup_one(str(src), str(dest))
        assert ok is True
        assert dest.exists()

    def test_atomic_replace_no_tmp_leftover(self, tmp_path):
        from backup_db import backup_one
        src = tmp_path / "s.db"
        _seed_db(str(src))
        dest = tmp_path / "out.db"
        backup_one(str(src), str(dest))
        # No .tmp file left behind
        assert not (tmp_path / "out.db.tmp").exists()


# ---------------------------------------------------------------------------
# backup_all
# ---------------------------------------------------------------------------

class TestBackupAll:
    def test_backs_up_every_db_in_dir(self, tmp_path):
        from backup_db import backup_all
        # Create 3 DBs + a non-DB file
        for n in ("a.db", "profile_1.db", "profile_2.db"):
            _seed_db(str(tmp_path / n))
        (tmp_path / "ignore.txt").write_text("not a db")

        backup_dir = tmp_path / "backups"
        summary = backup_all(str(tmp_path), str(backup_dir))
        assert summary["backed_up"] == 3
        assert summary["failed"] == 0
        assert len(summary["files"]) == 3

    def test_ignores_wal_and_shm(self, tmp_path):
        from backup_db import backup_all
        _seed_db(str(tmp_path / "real.db"))
        # WAL/SHM sidecars should be skipped, not treated as DBs
        (tmp_path / "real.db-wal").write_bytes(b"junk")
        (tmp_path / "real.db-shm").write_bytes(b"junk")

        backup_dir = tmp_path / "backups"
        summary = backup_all(str(tmp_path), str(backup_dir))
        assert summary["backed_up"] == 1


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------

class TestPruneOldBackups:
    def test_removes_files_older_than_retention(self, tmp_path):
        from backup_db import prune_old_backups

        # Create files with embedded date stamps (filename pattern is what
        # pruning matches; actual filesystem mtime is irrelevant)
        old_date = (datetime.utcnow().date() - timedelta(days=30)).strftime("%Y-%m-%d")
        new_date = datetime.utcnow().date().strftime("%Y-%m-%d")
        (tmp_path / f"db.{old_date}.db").write_bytes(b"old")
        (tmp_path / f"db.{new_date}.db").write_bytes(b"new")
        (tmp_path / "no-date-stamp.db").write_bytes(b"safe")

        removed = prune_old_backups(str(tmp_path), retain_days=14)
        assert removed == 1
        assert (tmp_path / f"db.{new_date}.db").exists()
        assert not (tmp_path / f"db.{old_date}.db").exists()
        # Files without a date stamp are left alone (defensive)
        assert (tmp_path / "no-date-stamp.db").exists()

    def test_zero_when_dir_empty(self, tmp_path):
        from backup_db import prune_old_backups
        assert prune_old_backups(str(tmp_path / "missing"), 14) == 0
        assert prune_old_backups(str(tmp_path), 14) == 0

    def test_full_cycle_with_backup_all(self, tmp_path):
        """backup_all must call prune_old_backups internally."""
        from backup_db import backup_all
        _seed_db(str(tmp_path / "live.db"))
        backup_dir = tmp_path / "b"
        backup_dir.mkdir()
        # Plant an old backup; it should be pruned this run
        old = (datetime.utcnow().date() - timedelta(days=60)).strftime("%Y-%m-%d")
        (backup_dir / f"live.{old}.db").write_bytes(b"old")

        summary = backup_all(str(tmp_path), str(backup_dir), retain_days=14)
        assert summary["pruned"] == 1
        assert not (backup_dir / f"live.{old}.db").exists()


class TestListBackups:
    def test_returns_metadata_newest_first(self, tmp_path):
        from backup_db import list_backups
        for d in ("2026-04-10", "2026-04-12", "2026-04-14"):
            (tmp_path / f"profile_1.{d}.db").write_bytes(b"x" * 1024)

        rows = list_backups(str(tmp_path))
        assert [r["date"] for r in rows] == ["2026-04-14", "2026-04-12", "2026-04-10"]
        for r in rows:
            assert r["size_bytes"] == 1024
            assert r["size_mb"] == round(1024 / (1024 * 1024), 2)
