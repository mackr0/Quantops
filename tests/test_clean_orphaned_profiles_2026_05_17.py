"""Tests for clean_orphaned_profiles.py (2026-05-17, batch C).

Covers:
  - profiles with valid alpaca_account_id are NOT touched
  - profiles whose alpaca_account_id is gone ARE flagged
  - profiles with NULL alpaca_account_id are NOT touched (legacy
    user-level keys are fine)
  - dry-run never modifies disk or DB
  - --apply backs up + removes the DB file + deletes the profile row
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


@pytest.fixture
def fake_world(tmp_path):
    """Build a quantopsai.db with 3 profiles:
    - pid=1 has live alpaca_account_id=10
    - pid=2 has orphaned alpaca_account_id=99 (no such row)
    - pid=3 has alpaca_account_id=NULL (legacy)
    Plus matching per-profile db files for #1 and #2."""
    main_db = tmp_path / "quantopsai.db"
    conn = sqlite3.connect(main_db)
    conn.executescript(
        """
        CREATE TABLE alpaca_accounts (
            id INTEGER PRIMARY KEY, user_id INTEGER
        );
        CREATE TABLE trading_profiles (
            id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT,
            alpaca_account_id INTEGER, enabled INTEGER DEFAULT 1
        );
        INSERT INTO alpaca_accounts (id, user_id) VALUES (10, 1);
        INSERT INTO trading_profiles (id, user_id, name,
            alpaca_account_id, enabled) VALUES
            (1, 1, 'Live Profile',     10,   1),
            (2, 1, 'Orphaned Profile', 99,   1),
            (3, 1, 'Legacy Profile',   NULL, 1);
        """
    )
    conn.commit()
    conn.close()

    # Per-profile DB files
    for pid in (1, 2):
        p = tmp_path / f"quantopsai_profile_{pid}.db"
        sqlite3.connect(p).close()

    return {
        "main_db": str(main_db),
        "tmp_path": tmp_path,
    }


def _run_main(monkeypatch, fake_world, argv):
    """Run clean_orphaned_profiles.main() with patched paths."""
    monkeypatch.chdir(fake_world["tmp_path"])
    monkeypatch.setattr(sys, "argv", ["clean_orphaned_profiles.py", *argv])
    # Pin the prod paths so they don't resolve to a real /opt path
    monkeypatch.setattr(
        "clean_orphaned_profiles._MAIN_DB_CANDIDATES",
        ("quantopsai.db",),  # local-dev only
    )
    monkeypatch.setattr(
        "clean_orphaned_profiles._BACKUP_ROOT",
        str(fake_world["tmp_path"] / "backups" / "pre-orphan-cleanup"),
    )
    # _per_profile_db_path checks the prod /opt path first; force
    # it to the local-dev path by monkeypatching os.path.exists for
    # the prod paths.
    real_exists = os.path.exists

    def _patched_exists(p):
        if isinstance(p, str) and p.startswith("/opt/quantopsai/"):
            return False
        return real_exists(p)

    monkeypatch.setattr(os.path, "exists", _patched_exists)

    import clean_orphaned_profiles
    return clean_orphaned_profiles.main()


class TestFindOrphans:
    def test_orphans_detected(self, fake_world):
        import clean_orphaned_profiles
        # Bypass _per_profile_db_path's /opt check by monkey-patching
        # the lookup explicitly for the test universe.
        with patch.object(
            clean_orphaned_profiles, "_per_profile_db_path",
            side_effect=lambda pid: str(fake_world["tmp_path"] /
                                        f"quantopsai_profile_{pid}.db"),
        ):
            orphans = clean_orphaned_profiles._find_orphans(
                fake_world["main_db"], user_id=1,
            )
        ids = {o["id"] for o in orphans}
        # Only pid=2 is orphaned: pid=1 has live acct=10, pid=3 has
        # alpaca_account_id=NULL (legacy, not orphaned).
        assert ids == {2}

    def test_no_orphans_when_all_accounts_live(self, tmp_path):
        import clean_orphaned_profiles
        main_db = tmp_path / "quantopsai.db"
        with sqlite3.connect(main_db) as conn:
            conn.executescript(
                """
                CREATE TABLE alpaca_accounts (
                    id INTEGER PRIMARY KEY, user_id INTEGER
                );
                CREATE TABLE trading_profiles (
                    id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT,
                    alpaca_account_id INTEGER, enabled INTEGER DEFAULT 1
                );
                INSERT INTO alpaca_accounts (id, user_id) VALUES
                    (10, 1), (11, 1);
                INSERT INTO trading_profiles (id, user_id, name,
                    alpaca_account_id, enabled) VALUES
                    (1, 1, 'P1', 10, 1),
                    (2, 1, 'P2', 11, 1);
                """
            )
        orphans = clean_orphaned_profiles._find_orphans(str(main_db), 1)
        assert orphans == []


class TestDryRun:
    def test_dry_run_no_changes(self, fake_world, monkeypatch):
        rc = _run_main(monkeypatch, fake_world, [])  # no --apply
        assert rc == 0
        # Profile #2 row still exists
        with sqlite3.connect(fake_world["main_db"]) as conn:
            row = conn.execute(
                "SELECT id FROM trading_profiles WHERE id = 2"
            ).fetchone()
            assert row is not None
        # Profile #2 DB file still exists
        assert (fake_world["tmp_path"] / "quantopsai_profile_2.db").exists()


class TestApply:
    def test_apply_removes_orphan_only(self, fake_world, monkeypatch):
        rc = _run_main(monkeypatch, fake_world, ["--apply"])
        assert rc == 0
        # Orphaned profile row gone, live + legacy still present
        with sqlite3.connect(fake_world["main_db"]) as conn:
            rows = {r[0] for r in conn.execute(
                "SELECT id FROM trading_profiles"
            ).fetchall()}
            assert rows == {1, 3}
        # Orphaned DB file gone, live DB file still present
        assert (fake_world["tmp_path"] / "quantopsai_profile_1.db").exists()
        assert not (
            fake_world["tmp_path"] / "quantopsai_profile_2.db"
        ).exists()
        # Backup exists for the removed file
        backup_root = (
            fake_world["tmp_path"] / "backups" / "pre-orphan-cleanup"
        )
        # Backup dir is timestamped — find the one created.
        backup_subdirs = list(backup_root.parent.glob("pre-orphan-cleanup-*"))
        assert len(backup_subdirs) == 1
        assert (backup_subdirs[0] / "quantopsai_profile_2.db").exists()

    def test_clear_audit_alerts_flag_truncates_table(
        self, fake_world, monkeypatch,
    ):
        """--clear-audit-alerts wipes audit_alerts even when no
        orphans exist. Use case: fresh-start reset after a previous
        run already removed the orphans."""
        import sqlite3
        # Seed an audit_alerts row
        with sqlite3.connect(fake_world["main_db"]) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS audit_alerts (
                    signature TEXT PRIMARY KEY,
                    audit_type TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    resolved_at TEXT,
                    details_json TEXT,
                    alert_sent INTEGER NOT NULL DEFAULT 0
                );
                INSERT INTO audit_alerts (signature, audit_type,
                    first_seen, last_seen, alert_sent) VALUES
                    ('qty_parity:10:AAPL', 'qty_parity',
                     '2026-05-17T00:00:00Z', '2026-05-17T00:00:00Z', 1);
            """)
        rc = _run_main(monkeypatch, fake_world,
                       ["--apply", "--clear-audit-alerts"])
        assert rc == 0
        with sqlite3.connect(fake_world["main_db"]) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM audit_alerts"
            ).fetchone()[0]
        assert count == 0

    def test_clear_audit_alerts_dry_run_doesnt_wipe(
        self, fake_world, monkeypatch,
    ):
        """Without --apply, audit_alerts must NOT be cleared."""
        import sqlite3
        with sqlite3.connect(fake_world["main_db"]) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS audit_alerts (
                    signature TEXT PRIMARY KEY,
                    audit_type TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    resolved_at TEXT,
                    details_json TEXT,
                    alert_sent INTEGER NOT NULL DEFAULT 0
                );
                INSERT INTO audit_alerts (signature, audit_type,
                    first_seen, last_seen, alert_sent) VALUES
                    ('value_parity:10', 'value_parity',
                     '2026-05-17T00:00:00Z', '2026-05-17T00:00:00Z', 0);
            """)
        # No --apply
        rc = _run_main(monkeypatch, fake_world, ["--clear-audit-alerts"])
        assert rc == 0
        with sqlite3.connect(fake_world["main_db"]) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM audit_alerts"
            ).fetchone()[0]
        assert count == 1  # still there

    def test_apply_no_orphans_is_noop(self, tmp_path, monkeypatch):
        """If there are no orphans, --apply returns 0 and changes nothing."""
        import clean_orphaned_profiles
        main_db = tmp_path / "quantopsai.db"
        with sqlite3.connect(main_db) as conn:
            conn.executescript(
                """
                CREATE TABLE alpaca_accounts (
                    id INTEGER PRIMARY KEY, user_id INTEGER
                );
                CREATE TABLE trading_profiles (
                    id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT,
                    alpaca_account_id INTEGER, enabled INTEGER DEFAULT 1
                );
                INSERT INTO alpaca_accounts (id, user_id) VALUES (10, 1);
                INSERT INTO trading_profiles (id, user_id, name,
                    alpaca_account_id, enabled) VALUES
                    (1, 1, 'Live', 10, 1);
                """
            )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(sys, "argv",
                            ["clean_orphaned_profiles.py", "--apply"])
        monkeypatch.setattr(
            clean_orphaned_profiles, "_MAIN_DB_CANDIDATES",
            ("quantopsai.db",),
        )
        rc = clean_orphaned_profiles.main()
        assert rc == 0
        with sqlite3.connect(main_db) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM trading_profiles"
            ).fetchone()[0] == 1
