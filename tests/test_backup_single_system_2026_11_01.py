"""2026-11-01 — one backup system, with a disk-headroom guard, and a
pre-market check that notices when the disk is filling.

Incident: the droplet's disk hit 100% on 2026-10-27 and stayed there
until 2026-11-01. Two backup systems (backup_daily.sh via cron and
`_task_db_backup` inside the scheduler) each copied every database
every day, the cron one also copied ~8GB of alt-data caches, and once
the disk was full gzip could not write its output either, so nothing
ever freed space again. The scheduler's file logging failed on every
write, every backup failed, SQLite threw disk-full errors, and the
first visible symptom was the DigitalOcean web console refusing to log
in (its agent could not write a login key).

Pinned here:
  * backup_daily.sh is the ONLY backup writer — the scheduler has no
    backup task and backup_db.py is gone.
  * housekeeping (compress, prune) runs BEFORE new snapshots are taken.
  * a snapshot is refused when it would leave less than the configured
    headroom; the run reports FAIL and exits non-zero; housekeeping
    still ran.
  * alt-data DBs are snapshotted on one weekday only.
  * every run writes a status summary.
  * premarket_smoke_test.py checks disk headroom and the last backup
    summary, first.
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import time
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "backup_daily.sh"

_SNAPSHOT_RE = re.compile(r"^(?P<db>.+\.db)\.(?P<ts>\d{8}-\d{4})(?P<gz>\.gz)?$")


def _seed_db(path: Path, rows: int = 5) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (k TEXT, v INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?, ?)", [(f"k{i}", i) for i in range(rows)])
    conn.commit()
    conn.close()


def _age(path: Path, days: float) -> None:
    ts = time.time() - days * 86400
    os.utime(path, (ts, ts))


def _snapshots(bdir: Path) -> dict:
    """{filename: match} for every snapshot-pattern file in the dir."""
    out = {}
    for p in bdir.iterdir():
        m = _SNAPSHOT_RE.match(p.name)
        if m:
            out[p.name] = m
    return out


def _status(bdir: Path) -> dict:
    text = (bdir / "last_run.status").read_text()
    return dict(line.split("=", 1) for line in text.strip().splitlines())


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _seed_db(root / "quantopsai.db", rows=7)
    _seed_db(root / "quantopsai_profile_1.db")
    _seed_db(root / "quantopsai_profile_2.db")
    alt = root / "altdata" / "stocktwits" / "data"
    alt.mkdir(parents=True)
    _seed_db(alt / "stocktwits.db", rows=50)
    return root


@pytest.fixture
def bdir(tmp_path):
    d = tmp_path / "backups"
    d.mkdir()
    return d


def _run(repo: Path, bdir: Path, **env_over) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "BACKUP_REPO_ROOT": str(repo),
        "BACKUP_DIR": str(bdir),
        "BACKUP_LOG_TAG": "quantopsai-backup-test",
        "BACKUP_MIN_FREE_GB": "0",      # tests must not depend on the dev box's disk
        "BACKUP_TODAY_DOW": "1",        # Monday unless a test says otherwise
        "BACKUP_RETAIN_DAYS": "14",
    }
    env.update({k: str(v) for k, v in env_over.items()})
    return subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=120,
    )


def test_tools_present_so_these_tests_actually_run():
    # The suite runs with 0 skipped; a missing tool is a failure, not a skip.
    assert shutil.which("bash"), "bash not found"
    assert shutil.which("sqlite3"), "sqlite3 CLI not found — backup_daily.sh needs it"
    assert shutil.which("gzip"), "gzip not found"
    assert shutil.which("bc"), "bc not found — backup_daily.sh uses it to format sizes"
    assert SCRIPT.exists()


# ── Script behaviour ─────────────────────────────────────────────────

class TestSnapshots:
    def test_irreplaceable_dbs_snapshotted_valid_and_altdata_skipped_on_a_weekday(self, repo, bdir):
        r = _run(repo, bdir)
        assert r.returncode == 0, r.stdout + r.stderr
        snaps = _snapshots(bdir)
        dbs = {m.group("db") for m in snaps.values()}
        assert dbs == {"quantopsai.db", "quantopsai_profile_1.db", "quantopsai_profile_2.db"}, dbs
        assert not any(m.group("gz") for m in snaps.values())
        # The master snapshot is a real, complete SQLite copy.
        master = next(n for n, m in snaps.items() if m.group("db") == "quantopsai.db")
        conn = sqlite3.connect(str(bdir / master))
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 7
        conn.close()
        st = _status(bdir)
        assert st["result"] == "OK"
        assert st["backed_up"] == "3"
        assert st["failed"] == "0"
        assert st["skipped_disk"] == "0"
        assert st["altdata_included"] == "no"
        assert "alt-data snapshots skipped today" in r.stdout

    def test_altdata_snapshotted_on_its_weekday(self, repo, bdir):
        r = _run(repo, bdir, BACKUP_TODAY_DOW="7")
        assert r.returncode == 0, r.stdout + r.stderr
        dbs = {m.group("db") for m in _snapshots(bdir).values()}
        assert "stocktwits.db" in dbs
        st = _status(bdir)
        assert st["altdata_included"] == "yes"
        assert st["backed_up"] == "4"

    def test_altdata_always_override(self, repo, bdir):
        r = _run(repo, bdir, BACKUP_TODAY_DOW="3", BACKUP_ALTDATA_DOW="always")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "stocktwits.db" in {m.group("db") for m in _snapshots(bdir).values()}

    def test_status_file_has_every_field_and_a_utc_timestamp(self, repo, bdir):
        _run(repo, bdir)
        st = _status(bdir)
        assert set(st) == {
            "result", "timestamp", "backed_up", "failed", "skipped_disk",
            "compressed", "pruned", "orphans_removed", "altdata_included",
            "free_after_bytes",
        }
        ran = datetime.strptime(st["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        assert abs((datetime.now(timezone.utc) - ran).total_seconds()) < 300
        assert int(st["free_after_bytes"]) > 0


class TestHousekeeping:
    def test_old_snapshots_compressed_and_expired_ones_pruned(self, repo, bdir):
        three_days = bdir / "quantopsai.db.20261029-0500"
        three_days.write_bytes(b"x" * 4096)
        _age(three_days, 3)
        fresh = bdir / "quantopsai_profile_1.db.20261101-0500"
        fresh.write_bytes(b"y" * 4096)                      # 0 days old — left alone
        expired_gz = bdir / "quantopsai_profile_1.db.20261001-0500.gz"
        expired_gz.write_bytes(b"z" * 10)
        _age(expired_gz, 20)
        expired_raw = bdir / "quantopsai_profile_2.db.20261002-0500"
        expired_raw.write_bytes(b"w" * 10)
        _age(expired_raw, 20)
        legacy = bdir / "handmade_copy.db"                  # not our pattern — never touched
        legacy.write_bytes(b"keep")

        r = _run(repo, bdir)
        assert r.returncode == 0, r.stdout + r.stderr
        assert not three_days.exists()
        assert (bdir / "quantopsai.db.20261029-0500.gz").exists()
        assert fresh.exists()
        assert not expired_gz.exists()
        assert not expired_raw.exists(), "a never-compressed expired snapshot must still be pruned"
        assert not (bdir / "quantopsai_profile_2.db.20261002-0500.gz").exists()
        assert legacy.exists()
        st = _status(bdir)
        assert st["compressed"] == "2"      # the 3-day-old and the 20-day-old raw file
        assert st["pruned"] == "2"

    def test_orphaned_sidecars_removed_but_live_ones_kept(self, repo, bdir):
        orphan = bdir / "quantopsai.db.20261020-0500-shm"
        orphan.write_bytes(b"s")
        parent = bdir / "quantopsai_profile_1.db.20261101-0500"
        parent.write_bytes(b"p")
        kept = bdir / "quantopsai_profile_1.db.20261101-0500-wal"
        kept.write_bytes(b"w")
        _run(repo, bdir)
        assert not orphan.exists()
        assert kept.exists()
        assert _status(bdir)["orphans_removed"] == "1"


class TestDiskHeadroomGuard:
    def test_snapshots_refused_run_fails_loudly_and_housekeeping_still_ran(self, repo, bdir):
        old = bdir / "quantopsai.db.20261029-0500"
        old.write_bytes(b"x" * 4096)
        _age(old, 3)

        r = _run(repo, bdir, BACKUP_MIN_FREE_GB="100000")   # ~100 PB floor: nothing fits
        assert r.returncode == 1, "a refused snapshot must make the run exit non-zero"
        new = [n for n, m in _snapshots(bdir).items() if not m.group("gz")]
        assert new == [], f"no new snapshot may be written without headroom, got {new}"
        assert "disk headroom" in r.stdout
        assert "FAIL" in r.stdout
        # Housekeeping ran first and freed what it could.
        assert not old.exists()
        assert (bdir / "quantopsai.db.20261029-0500.gz").exists()
        st = _status(bdir)
        assert st["result"] == "FAIL"
        assert st["backed_up"] == "0"
        assert st["failed"] == "3"
        assert st["skipped_disk"] == "3"
        assert st["compressed"] == "1"

    def test_floor_is_five_gb_in_production(self):
        src = SCRIPT.read_text()
        assert 'MIN_FREE_GB="${BACKUP_MIN_FREE_GB:-5}"' in src
        assert 'ALTDATA_DOW="${BACKUP_ALTDATA_DOW:-7}"' in src, "alt-data is weekly (Sunday) by default"

    def test_housekeeping_is_ordered_before_snapshots(self):
        src = SCRIPT.read_text()
        first_backup_call = src.index('\nbackup_one "${REPO_ROOT}/quantopsai.db"')
        assert src.index('log "compressed ${COMPRESSED} backups"') < first_backup_call
        assert src.index('log "pruned ${PRUNED} backups') < first_backup_call


# ── One backup system ────────────────────────────────────────────────

class TestSingleBackupSystem:
    def test_scheduler_has_no_backup_task(self):
        src = (REPO / "multi_scheduler.py").read_text()
        assert "_task_db_backup" not in src
        assert "from backup_db import" not in src
        assert 'DB Backup"' not in src, "no run_task named DB Backup"

    def test_in_app_backup_module_is_gone(self):
        assert not (REPO / "backup_db.py").exists()
        assert not (REPO / "tests" / "test_backup_db.py").exists()

    def test_no_production_module_writes_a_second_backup_location(self):
        offenders = []
        for p in REPO.glob("*.py"):
            if "/var/backups" in p.read_text(errors="replace"):
                offenders.append(p.name)
        assert offenders == [], (
            f"{offenders} reference /var/backups — the second backup location "
            "that helped fill the disk. backup_daily.sh is the one backup system."
        )

    def test_cron_docs_name_backup_daily_only(self):
        ops = (REPO / "docs" / "07_OPERATIONS.md").read_text()
        assert "backup_daily.sh" in ops
        assert "last_run.status" in ops
        assert "_task_db_backup" not in ops


# ── Pre-market smoke checks ──────────────────────────────────────────

_Usage = namedtuple("usage", "total used free")
_GB = 1024 ** 3


class TestSmokeDiskHeadroom:
    def test_registered_first(self):
        import premarket_smoke_test as smoke
        names = [n for n, _ in smoke.CHECKS]
        assert names[:2] == ["disk_headroom", "last_backup_ok"], names

    def test_fails_below_five_gb(self, monkeypatch):
        import premarket_smoke_test as smoke
        monkeypatch.setattr(smoke.shutil, "disk_usage", lambda p: _Usage(58 * _GB, 55 * _GB, 3 * _GB))
        ok, detail = smoke.check_disk_headroom()
        assert ok is False
        assert "disk nearly full" in detail
        assert "3.2 GB free" in detail

    def test_fails_below_ten_percent_even_when_more_than_five_gb(self, monkeypatch):
        import premarket_smoke_test as smoke
        monkeypatch.setattr(smoke.shutil, "disk_usage", lambda p: _Usage(200 * _GB, 192 * _GB, 8 * _GB))
        ok, detail = smoke.check_disk_headroom()
        assert ok is False

    def test_passes_with_room(self, monkeypatch):
        import premarket_smoke_test as smoke
        monkeypatch.setattr(smoke.shutil, "disk_usage", lambda p: _Usage(58 * _GB, 30 * _GB, 28 * _GB))
        ok, detail = smoke.check_disk_headroom()
        assert ok is True
        assert "GB free" in detail

    def test_the_real_check_runs_without_crashing(self):
        import premarket_smoke_test as smoke
        ok, detail = smoke.check_disk_headroom()
        assert isinstance(ok, bool)
        assert "GB free" in detail


def _write_status(path: Path, **over) -> None:
    fields = {
        "result": "OK",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "backed_up": "13", "failed": "0", "skipped_disk": "0", "compressed": "12",
        "pruned": "13", "orphans_removed": "0", "altdata_included": "no",
        "free_after_bytes": str(20 * _GB),
    }
    fields.update(over)
    path.write_text("".join(f"{k}={v}\n" for k, v in fields.items()))


class TestSmokeLastBackup:
    def test_missing_summary_fails(self, tmp_path, monkeypatch):
        import premarket_smoke_test as smoke
        monkeypatch.setattr(smoke, "BACKUP_STATUS_FILE", str(tmp_path / "none.status"))
        ok, detail = smoke.check_last_backup_ok()
        assert ok is False
        assert "no backup summary" in detail

    def test_fresh_clean_run_passes(self, tmp_path, monkeypatch):
        import premarket_smoke_test as smoke
        p = tmp_path / "last_run.status"
        _write_status(p)
        monkeypatch.setattr(smoke, "BACKUP_STATUS_FILE", str(p))
        ok, detail = smoke.check_last_backup_ok()
        assert ok is True, detail
        assert "13 databases saved" in detail
        assert "21.5 GB free afterwards" in detail

    def test_stale_run_fails(self, tmp_path, monkeypatch):
        import premarket_smoke_test as smoke
        p = tmp_path / "last_run.status"
        old = (datetime.now(timezone.utc) - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _write_status(p, timestamp=old)
        monkeypatch.setattr(smoke, "BACKUP_STATUS_FILE", str(p))
        ok, detail = smoke.check_last_backup_ok()
        assert ok is False
        assert "30 hours ago" in detail

    def test_failed_run_fails_and_names_disk_when_that_was_why(self, tmp_path, monkeypatch):
        import premarket_smoke_test as smoke
        p = tmp_path / "last_run.status"
        _write_status(p, result="FAIL", backed_up="0", failed="13", skipped_disk="13")
        monkeypatch.setattr(smoke, "BACKUP_STATUS_FILE", str(p))
        ok, detail = smoke.check_last_backup_ok()
        assert ok is False
        assert "FAILED" in detail
        assert "13 snapshot(s) refused for lack of disk space" in detail

    def test_unreadable_summary_fails_not_crashes(self, tmp_path, monkeypatch):
        import premarket_smoke_test as smoke
        p = tmp_path / "last_run.status"
        p.write_text("garbage\n")
        monkeypatch.setattr(smoke, "BACKUP_STATUS_FILE", str(p))
        ok, detail = smoke.check_last_backup_ok()
        assert ok is False
        assert "unreadable" in detail

    def test_end_to_end_script_summary_satisfies_the_smoke_check(self, repo, bdir, monkeypatch):
        """The script's real output is what the smoke check reads."""
        import premarket_smoke_test as smoke
        r = _run(repo, bdir)
        assert r.returncode == 0, r.stdout + r.stderr
        monkeypatch.setattr(smoke, "BACKUP_STATUS_FILE", str(bdir / "last_run.status"))
        ok, detail = smoke.check_last_backup_ok()
        assert ok is True, detail
        assert "3 databases saved, 0 failures" in detail
