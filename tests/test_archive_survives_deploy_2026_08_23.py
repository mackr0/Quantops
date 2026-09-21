"""The learning archive must survive a deploy (2026-08-23 incident).

sync.sh rsyncs the repo to prod with --delete, excluding backups/ but
— until today — not predictions_archive/. The first deploy after the
Experiment 1 reset deleted the 170,536-row archive the reset had just
written (recovered from the pre-wipe DB backups). Pins:
  - the deploy cannot delete ANYTHING on the droplet (2026-09-21: the
    exclude list this test used to pin was replaced by an allowlist —
    sync.sh ships `git ls-files` with no --delete, so the archive, which
    git does not track, is out of its reach by construction; the full
    pins live in test_deploy_ships_only_tracked_files_2026_09_21.py);
  - the archive trees are not tracked by git (or a deploy would
    overwrite them);
  - every archive default root lives under backups/;
  - the archive includes the shadow-model rows (the challengers'
    evaluation record) alongside predictions.
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class TestDeployCannotReachTheArchive:
    def test_the_deploy_never_deletes_on_the_droplet(self):
        src = open(os.path.join(ROOT, "sync.sh")).read()
        code = [ln for ln in src.splitlines()
                if not ln.lstrip().startswith("#")]
        assert not [ln for ln in code if "--delete" in ln]
        assert '--files-from="$SHIP_LIST"' in src

    def test_the_archive_trees_are_not_tracked_by_git(self):
        """The deploy ships exactly what git tracks; a tracked file
        under these trees would be overwritten on every deploy."""
        import subprocess
        tracked = subprocess.run(
            ["git", "ls-files", "--", "backups", "predictions_archive"],
            cwd=ROOT, capture_output=True, text=True, check=True).stdout
        assert tracked.strip() == "", tracked


class TestArchiveRoots:
    def test_default_roots_live_under_backups(self):
        import predictions_archive as pa
        from finetune import dataset_builder as db
        import inspect
        assert pa.DEFAULT_ARCHIVE_ROOT.startswith("backups/")
        assert inspect.signature(pa.archive_predictions).parameters[
            "archive_root"].default.startswith("backups/")
        assert inspect.signature(pa.archive_all_active_profiles).parameters[
            "archive_root"].default.startswith("backups/")
        assert inspect.signature(db.build_dataset).parameters[
            "archive_root"].default.startswith("backups/")

    def test_reset_script_archives_under_backups(self):
        src = open(os.path.join(ROOT, "full_fresh_start_2026_08_24.py")).read()
        idx = src.index("def step1c_archive_learning_data")
        body = src[idx:idx + 2500]
        assert "backups/predictions_archive" in body
        assert 'f"{REPO_ROOT}/predictions_archive"' not in body


class TestArchiveContents:
    def test_shadow_calls_are_archived(self, tmp_path):
        import json
        from predictions_archive import archive_predictions
        db = str(tmp_path / "quantopsai_profile_77.db")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE ai_predictions (id INTEGER PRIMARY KEY, symbol TEXT)")
        conn.execute("INSERT INTO ai_predictions (symbol) VALUES ('AAPL')")
        conn.execute("CREATE TABLE ai_shadow_calls (id INTEGER PRIMARY KEY, model TEXT, parsed_signal TEXT)")
        conn.execute("INSERT INTO ai_shadow_calls (model, parsed_signal) VALUES ('gpt-4.1-nano', 'BUY')")
        conn.commit(); conn.close()
        counts = archive_predictions(db, 77, archive_root=str(tmp_path / "arch"),
                                     reset_timestamp="t")
        assert counts["predictions"] == 1 and counts["shadow_calls"] == 1
        row = json.loads(open(tmp_path / "arch" / "77" / "t" / "shadow_calls.jsonl").readline())
        assert row["model"] == "gpt-4.1-nano"
