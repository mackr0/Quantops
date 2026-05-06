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
    """Selection is by mtime — the most recently created backup wins,
    regardless of the filename's encoded date. backup_daily.sh creates
    one file per run so mtime tracks creation."""
    import time
    from db_integrity import find_latest_backup
    bk = tmp_path / "backups"
    bk.mkdir()
    f1 = bk / "quantopsai.db.20260503"
    f2 = bk / "quantopsai.db.20260504"
    f3 = bk / "quantopsai.db.20260502"
    f1.write_text("a")
    f2.write_text("b")
    f3.write_text("c")
    # Make .20260504 the most recently modified
    now = time.time()
    os.utime(str(f1), (now - 200, now - 200))
    os.utime(str(f2), (now, now))
    os.utime(str(f3), (now - 100, now - 100))
    latest = find_latest_backup("quantopsai.db", backup_dir=str(bk))
    assert latest is not None
    assert latest.endswith(".20260504")


def test_find_latest_backup_returns_none_when_missing(tmp_path):
    from db_integrity import find_latest_backup
    assert find_latest_backup(
        "quantopsai.db", backup_dir=str(tmp_path / "nope"),
    ) is None


def test_find_latest_backup_legacy_underscore_naming(tmp_path):
    """Hand-named ad-hoc snapshots use `<basename>_<YYYY-MM-DD>_<HHMM>.db`.
    Discovered on prod 2026-05-05 — the original glob `<filename>.*`
    didn't match these and restore would fail to find them."""
    from db_integrity import find_latest_backup
    bk = tmp_path / "backups"
    bk.mkdir()
    (bk / "quantopsai_2026-04-22_2054.db").write_text("a")
    latest = find_latest_backup("quantopsai.db", backup_dir=str(bk))
    assert latest is not None
    assert latest.endswith("quantopsai_2026-04-22_2054.db")


def test_find_latest_backup_master_does_not_match_profile(tmp_path):
    """Lookup of `quantopsai.db` must NOT pick up a profile DB file
    whose name happens to start with `quantopsai_`."""
    from db_integrity import find_latest_backup
    bk = tmp_path / "backups"
    bk.mkdir()
    # Only profile files exist
    (bk / "quantopsai_profile_10_2026-04-22_2054.db").write_text("a")
    (bk / "quantopsai_profile_11_2026-04-22_2054.db").write_text("b")
    latest = find_latest_backup("quantopsai.db", backup_dir=str(bk))
    assert latest is None


def test_find_latest_backup_picks_by_mtime_across_naming(tmp_path):
    """Mixed legacy + new naming: pick the most recent by mtime,
    not lexical order."""
    import time
    from db_integrity import find_latest_backup
    bk = tmp_path / "backups"
    bk.mkdir()
    older = bk / "quantopsai.db.20260101-0500"
    older.write_text("old")
    newer = bk / "quantopsai_2026-04-22_2054.db"
    newer.write_text("newer")
    # Make `newer` actually newer on disk
    now = time.time()
    os.utime(str(older), (now - 86400, now - 86400))
    os.utime(str(newer), (now, now))
    latest = find_latest_backup("quantopsai.db", backup_dir=str(bk))
    assert latest is not None
    assert latest.endswith("quantopsai_2026-04-22_2054.db")


def test_find_latest_backup_for_profile_db(tmp_path):
    """`quantopsai_profile_10.db` lookup finds its dated snapshots."""
    from db_integrity import find_latest_backup
    bk = tmp_path / "backups"
    bk.mkdir()
    (bk / "quantopsai_profile_10_2026-04-22_2054.db").write_text("a")
    (bk / "quantopsai_profile_10.db.20260505-1200").write_text("b")
    latest = find_latest_backup(
        "quantopsai_profile_10.db", backup_dir=str(bk),
    )
    assert latest is not None


def test_find_latest_backup_excludes_wal_and_shm_sidecars(tmp_path):
    """Caught during 2026-05-05 prod rehearsal: SQLite creates `-wal`
    and `-shm` sidecars next to a backup file when something opens it
    without immutable=1. The previous glob `<filename>.*` matched
    `quantopsai.db.20260506-0014-wal` (0 bytes!), restore copied that
    over the live path, and check_db said 'ok' because empty files
    pass quick_check. find_latest_backup must reject sidecars."""
    import time
    from db_integrity import find_latest_backup
    bk = tmp_path / "backups"
    bk.mkdir()
    real = bk / "quantopsai.db.20260506-0014"
    real.write_text("a" * 5000)
    wal = bk / "quantopsai.db.20260506-0014-wal"
    wal.write_text("")  # 0 bytes
    shm = bk / "quantopsai.db.20260506-0014-shm"
    shm.write_text("x" * 32768)
    # Make sidecars NEWER than the real backup (worst case for mtime sort)
    now = time.time()
    os.utime(str(real), (now - 60, now - 60))
    os.utime(str(wal), (now, now))
    os.utime(str(shm), (now, now))
    latest = find_latest_backup("quantopsai.db", backup_dir=str(bk))
    assert latest is not None
    assert latest.endswith(".20260506-0014"), (
        f"picked sidecar instead of real backup: {latest}"
    )


def test_find_latest_backup_excludes_corrupt_archive(tmp_path):
    """restore_from_backup archives the corrupt original as
    `<filename>.corrupt-<TS>`. find_latest_backup must NOT pick that
    up — it's the bad data we just rejected. Otherwise the next
    restore loops on its own archive."""
    import time
    from db_integrity import find_latest_backup
    bk = tmp_path / "backups"
    bk.mkdir()
    good = bk / "quantopsai.db.20260506-0014"
    good.write_text("good")
    corrupt = bk / "quantopsai.db.corrupt-20260506-001517"
    corrupt.write_text("bad")
    # corrupt archive newer (it would be — restore happens after backup)
    now = time.time()
    os.utime(str(good), (now - 60, now - 60))
    os.utime(str(corrupt), (now, now))
    latest = find_latest_backup("quantopsai.db", backup_dir=str(bk))
    assert latest is not None
    assert "corrupt" not in os.path.basename(latest)


def test_check_db_zero_byte_file_is_corrupt(tmp_path):
    """SQLite happily opens a 0-byte file as a valid empty DB and
    quick_check returns ok. That's how a buggy restore could 'succeed'
    by copying a 0-byte WAL sidecar over the live path. check_db must
    catch it via the file-size / magic-header pre-check."""
    from db_integrity import check_db
    p = tmp_path / "empty.db"
    p.write_text("")
    out = check_db(str(p))
    assert out["status"] == "corrupt"
    assert "0 bytes" in out["detail"] or "header" in out["detail"]


def test_check_db_non_sqlite_file_is_corrupt(tmp_path):
    """File with content but no SQLite header magic — not a real DB."""
    from db_integrity import check_db
    p = tmp_path / "fake.db"
    p.write_bytes(b"not a sqlite db, just text" * 100)
    out = check_db(str(p))
    assert out["status"] == "corrupt"
    assert "header" in out["detail"]


def test_check_db_does_not_create_sidecars(tmp_path):
    """check_db must use immutable=1 so it does not create -wal or
    -shm files next to the file being inspected. Otherwise the act of
    verifying a backup pollutes the backup directory."""
    from db_integrity import check_db
    p = tmp_path / "real.db"
    # Create a real WAL-mode SQLite DB
    conn = sqlite3.connect(str(p))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()
    # Clean any sidecars left from setup
    for ext in ("-wal", "-shm"):
        side = tmp_path / f"real.db{ext}"
        if side.exists():
            side.unlink()
    # Run check_db
    out = check_db(str(p))
    assert out["status"] == "ok"
    # No sidecars should have been created
    assert not (tmp_path / "real.db-wal").exists(), "check_db created -wal sidecar"
    assert not (tmp_path / "real.db-shm").exists(), "check_db created -shm sidecar"


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
    # Real DB with content — passes the file-size + magic-header pre-check.
    _make_healthy_db(p)

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
    _make_healthy_db(p)

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
