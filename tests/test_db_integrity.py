"""DB integrity check + restore-from-backup tests.

Doomsday gap: SQLite is durable but if `quantopsai.db` corrupts mid-
write, the scheduler should halt + alert + restore from
backup_daily.sh's nightly snapshot, not silently mis-record fills.
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _make_healthy_db(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()


def _make_corrupt_db(path):
    """Create a SQLite file then truncate it mid-page so PRAGMA
    integrity_check fails."""
    _make_healthy_db(path)
    # Truncate to ~half the page size, which guarantees corruption
    size = os.path.getsize(path)
    with open(path, "r+b") as f:
        f.truncate(size // 2)


def test_check_db_ok(tmp_path):
    from db_integrity import check_db
    p = str(tmp_path / "ok.db")
    _make_healthy_db(p)
    out = check_db(p)
    assert out["status"] == "ok"


def test_check_db_missing(tmp_path):
    from db_integrity import check_db
    out = check_db(str(tmp_path / "nope.db"))
    assert out["status"] == "missing"


def test_check_db_corrupt(tmp_path):
    from db_integrity import check_db
    p = str(tmp_path / "corrupt.db")
    _make_corrupt_db(p)
    out = check_db(p)
    assert out["status"] == "corrupt"


def test_check_all_dbs_finds_master(tmp_path):
    from db_integrity import check_all_dbs
    _make_healthy_db(str(tmp_path / "quantopsai.db"))
    _make_healthy_db(str(tmp_path / "quantopsai_profile_1.db"))
    _make_healthy_db(str(tmp_path / "quantopsai_profile_2.db"))
    results = check_all_dbs(repo_root=str(tmp_path))
    assert "quantopsai.db" in results
    assert "quantopsai_profile_1.db" in results
    assert all(r["status"] == "ok" for r in results.values())


def test_any_corrupt_reports_bad_dbs(tmp_path):
    from db_integrity import check_all_dbs, any_corrupt
    _make_healthy_db(str(tmp_path / "quantopsai.db"))
    _make_corrupt_db(str(tmp_path / "quantopsai_profile_1.db"))
    results = check_all_dbs(repo_root=str(tmp_path))
    bad = any_corrupt(results)
    assert "quantopsai_profile_1.db" in bad
    assert "quantopsai.db" not in bad


def test_find_latest_backup(tmp_path):
    from db_integrity import find_latest_backup
    bk = tmp_path / "backups"
    bk.mkdir()
    # backup_daily.sh names files like quantopsai.db.20260503
    (bk / "quantopsai.db.20260503").write_text("a")
    (bk / "quantopsai.db.20260504").write_text("b")
    (bk / "quantopsai.db.20260502").write_text("c")
    latest = find_latest_backup("quantopsai.db", backup_dir=str(bk))
    assert latest is not None
    assert latest.endswith(".20260504")


def test_find_latest_backup_returns_none_when_missing(tmp_path):
    from db_integrity import find_latest_backup
    assert find_latest_backup(
        "quantopsai.db", backup_dir=str(tmp_path / "nope"),
    ) is None


def test_restore_from_backup_dry_run(tmp_path):
    """dry_run=True returns the action plan without touching files."""
    from db_integrity import restore_from_backup
    bk = tmp_path / "backups"
    bk.mkdir()
    backup = bk / "quantopsai.db.20260504"
    _make_healthy_db(str(backup))
    # Live file doesn't exist yet — that's fine, restore would create it.
    out = restore_from_backup(
        "quantopsai.db", repo_root=str(tmp_path),
        backup_dir=str(bk), dry_run=True,
    )
    assert out["status"] == "ok"
    assert out["from_backup"].endswith(".20260504")


def test_restore_from_backup_replaces_corrupt_file(tmp_path):
    from db_integrity import restore_from_backup, check_db
    bk = tmp_path / "backups"
    bk.mkdir()
    backup = bk / "quantopsai.db.20260504"
    _make_healthy_db(str(backup))
    # Live DB is corrupt
    live = tmp_path / "quantopsai.db"
    _make_corrupt_db(str(live))
    out = restore_from_backup(
        "quantopsai.db", repo_root=str(tmp_path),
        backup_dir=str(bk),
    )
    assert out["status"] == "ok"
    # Live should now be healthy
    assert check_db(str(live))["status"] == "ok"
    # Corrupt original archived
    archived = list(tmp_path.glob("quantopsai.db.corrupt-*"))
    assert len(archived) == 1


def test_restore_refuses_corrupt_backup(tmp_path):
    """If the backup itself is corrupt, refuse to restore — don't
    overwrite the live file with bad data."""
    from db_integrity import restore_from_backup
    bk = tmp_path / "backups"
    bk.mkdir()
    backup = bk / "quantopsai.db.20260504"
    _make_corrupt_db(str(backup))
    out = restore_from_backup(
        "quantopsai.db", repo_root=str(tmp_path),
        backup_dir=str(bk),
    )
    assert out["status"] == "error"
    assert "corrupt" in out["detail"].lower()


def test_null_in_not_null_is_treated_as_ok(tmp_path, monkeypatch):
    """The exact regression that took the scheduler down 2026-05-05:
    PRAGMA quick_check returns `NULL value in <table>.<col>` rows
    when pre-existing rows have NULL in a NOT NULL column added
    later. That's NOT file corruption and should NOT halt the
    scheduler. This test patches the PRAGMA call to return that
    pattern and asserts check_db returns 'ok'."""
    from db_integrity import check_db
    import db_integrity
    p = str(tmp_path / "test.db")
    # Create a healthy file
    sqlite3.connect(p).close()

    class _FakeConn:
        def execute(self, sql):
            class _Cur:
                def fetchall(self):
                    return [
                        ("NULL value in trading_profiles.foo",),
                        ("NULL value in trading_profiles.bar",),
                    ]
            return _Cur()
        def close(self):
            pass

    monkeypatch.setattr(
        db_integrity.sqlite3, "connect",
        lambda *a, **kw: _FakeConn(),
    )
    out = check_db(p)
    assert out["status"] == "ok", f"got {out}"


def test_real_corruption_still_halts(tmp_path, monkeypatch):
    """Index corruption / page errors / etc. ARE real corruption and
    must still halt."""
    from db_integrity import check_db
    import db_integrity
    p = str(tmp_path / "test.db")
    sqlite3.connect(p).close()

    class _FakeConn:
        def execute(self, sql):
            class _Cur:
                def fetchall(self):
                    return [
                        ("*** in database main ***",),
                        ("Page 5 is never used",),
                    ]
            return _Cur()
        def close(self):
            pass

    monkeypatch.setattr(
        db_integrity.sqlite3, "connect",
        lambda *a, **kw: _FakeConn(),
    )
    out = check_db(p)
    assert out["status"] == "corrupt"


def test_restore_returns_error_when_no_backup(tmp_path):
    from db_integrity import restore_from_backup
    out = restore_from_backup(
        "quantopsai.db", repo_root=str(tmp_path),
        backup_dir=str(tmp_path / "no_such_dir"),
    )
    assert out["status"] == "error"
    assert "no backup" in out["detail"].lower()
