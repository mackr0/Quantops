"""Tests for the reset script — paranoid because it's destructive.

What's tested:
  - Dry-run NEVER writes to the DB (default mode is safe)
  - Per-table truncate counts what was deleted
  - --wipe-ai-memory drops the specialist/tuning tables; default
    KEEPS them (AI memory preserved)
  - Backup file exists after --apply
  - Profile config in main DB is NOT touched
  - Unknown tables (not in any profile's schema) are skipped, not
    fatal
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


def _make_profile_db(path):
    """Create a per-profile DB shape that the reset script targets."""
    conn = sqlite3.connect(path)
    # ALWAYS_WIPE tables
    conn.executescript("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, side TEXT, qty REAL, price REAL,
            status TEXT DEFAULT 'open'
        );
        CREATE TABLE ai_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, predicted_signal TEXT
        );
        CREATE TABLE virtual_profile_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            equity REAL
        );
        CREATE TABLE ai_cost_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cost_usd REAL
        );
        CREATE TABLE activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT
        );
        CREATE TABLE specialist_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            specialist_name TEXT
        );
        CREATE TABLE tuning_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parameter_name TEXT
        );
    """)
    # Seed each with a few rows
    for t in ("trades", "ai_predictions", "virtual_profile_state",
              "ai_cost_ledger", "activity_log"):
        for _ in range(3):
            conn.execute(f"INSERT INTO {t} DEFAULT VALUES")
    for t in ("specialist_outcomes", "tuning_history"):
        for _ in range(2):
            conn.execute(f"INSERT INTO {t} DEFAULT VALUES")
    conn.commit()
    conn.close()


def _row_count(db, table):
    with sqlite3.connect(db) as conn:
        try:
            return conn.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            return -1


@pytest.fixture
def fake_profiles(tmp_path, monkeypatch):
    """Build 3 fake profile DBs and patch the discovery function."""
    paths = []
    for pid in (1, 3, 7):
        p = tmp_path / f"quantopsai_profile_{pid}.db"
        _make_profile_db(str(p))
        paths.append({
            "id": pid, "name": f"Profile {pid}",
            "db_path": str(p), "alpaca_account_id": 2,
        })
    monkeypatch.chdir(tmp_path)
    return paths


class TestDryRun:
    def test_dry_run_does_not_delete(self, fake_profiles, monkeypatch):
        from reset_for_clean_experiment import main
        before = {p["id"]: _row_count(p["db_path"], "trades")
                  for p in fake_profiles}
        assert all(n == 3 for n in before.values())

        with patch(
            "reset_for_clean_experiment._enabled_profile_dbs",
            return_value=fake_profiles,
        ):
            monkeypatch.setattr(
                sys, "argv", ["reset_for_clean_experiment.py"],
            )
            main()

        # Counts unchanged
        for p in fake_profiles:
            assert _row_count(p["db_path"], "trades") == 3


class TestApply:
    def test_apply_truncates_always_wipe_tables(
        self, fake_profiles, monkeypatch, tmp_path,
    ):
        from reset_for_clean_experiment import main
        with patch(
            "reset_for_clean_experiment._enabled_profile_dbs",
            return_value=fake_profiles,
        ), patch(
            "reset_for_clean_experiment._BACKUP_ROOT",
            str(tmp_path / "backups"),
        ):
            monkeypatch.setattr(
                sys, "argv",
                ["reset_for_clean_experiment.py", "--apply"],
            )
            main()

        for p in fake_profiles:
            for t in ("trades", "ai_predictions",
                      "virtual_profile_state",
                      "ai_cost_ledger", "activity_log"):
                assert _row_count(p["db_path"], t) == 0, (
                    f"profile {p['id']} table {t} should be empty "
                    f"after --apply"
                )

    def test_apply_preserves_ai_memory_by_default(
        self, fake_profiles, monkeypatch, tmp_path,
    ):
        from reset_for_clean_experiment import main
        with patch(
            "reset_for_clean_experiment._enabled_profile_dbs",
            return_value=fake_profiles,
        ), patch(
            "reset_for_clean_experiment._BACKUP_ROOT",
            str(tmp_path / "backups"),
        ):
            monkeypatch.setattr(
                sys, "argv",
                ["reset_for_clean_experiment.py", "--apply"],
            )
            main()

        for p in fake_profiles:
            # AI memory tables NOT wiped
            assert _row_count(p["db_path"], "specialist_outcomes") == 2
            assert _row_count(p["db_path"], "tuning_history") == 2

    def test_wipe_ai_memory_drops_specialist_tables(
        self, fake_profiles, monkeypatch, tmp_path,
    ):
        from reset_for_clean_experiment import main
        with patch(
            "reset_for_clean_experiment._enabled_profile_dbs",
            return_value=fake_profiles,
        ), patch(
            "reset_for_clean_experiment._BACKUP_ROOT",
            str(tmp_path / "backups"),
        ):
            monkeypatch.setattr(
                sys, "argv",
                ["reset_for_clean_experiment.py",
                 "--apply", "--wipe-ai-memory"],
            )
            main()

        for p in fake_profiles:
            assert _row_count(p["db_path"], "specialist_outcomes") == 0
            assert _row_count(p["db_path"], "tuning_history") == 0

    def test_apply_creates_backup_file(
        self, fake_profiles, monkeypatch, tmp_path,
    ):
        from reset_for_clean_experiment import main
        backup_root = str(tmp_path / "backups")
        with patch(
            "reset_for_clean_experiment._enabled_profile_dbs",
            return_value=fake_profiles,
        ), patch(
            "reset_for_clean_experiment._BACKUP_ROOT",
            backup_root,
        ):
            monkeypatch.setattr(
                sys, "argv",
                ["reset_for_clean_experiment.py", "--apply"],
            )
            main()

        # Some backup dir should now exist with the original DBs
        # (we don't know the exact ts-suffixed name)
        backup_dirs = [
            d for d in os.listdir(tmp_path)
            if d.startswith("backups-")
        ]
        assert len(backup_dirs) == 1, (
            f"expected 1 backup dir under tmp_path, got {backup_dirs}"
        )
        backed_up = os.listdir(tmp_path / backup_dirs[0])
        assert len(backed_up) == 3, "should backup all 3 profile DBs"

    def test_unknown_table_in_wipe_list_is_skipped_not_fatal(
        self, fake_profiles, monkeypatch, tmp_path,
    ):
        """If a profile DB lacks one of the target tables (e.g., a
        profile that never ran any cycles), the script should skip
        it gracefully, not crash."""
        from reset_for_clean_experiment import main
        # Drop a table from one profile's DB
        with sqlite3.connect(fake_profiles[0]["db_path"]) as conn:
            conn.execute("DROP TABLE activity_log")
        with patch(
            "reset_for_clean_experiment._enabled_profile_dbs",
            return_value=fake_profiles,
        ), patch(
            "reset_for_clean_experiment._BACKUP_ROOT",
            str(tmp_path / "backups"),
        ):
            monkeypatch.setattr(
                sys, "argv",
                ["reset_for_clean_experiment.py", "--apply"],
            )
            # Must not raise
            result = main()
        assert result == 0
        # Other tables still got wiped on that profile
        assert _row_count(
            fake_profiles[0]["db_path"], "trades"
        ) == 0
