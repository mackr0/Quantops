"""The learning archive must survive a deploy (2026-08-23 incident).

sync.sh rsyncs the repo to prod with --delete, excluding backups/ but
— until today — not predictions_archive/. The first deploy after the
Experiment 1 reset deleted the 170,536-row archive the reset had just
written (recovered from the pre-wipe DB backups). Pins:
  - both rsync invocations in sync.sh exclude backups/ AND
    predictions_archive/;
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


class TestSyncExcludes:
    def test_every_rsync_block_excludes_the_data_trees(self):
        src = open(os.path.join(ROOT, "sync.sh")).read()
        blocks = re.findall(r"rsync -az --delete.*?/Users/mackr0/Quantops/",
                            src, flags=re.S)
        assert len(blocks) >= 2, "expected the dry-run and the real rsync"
        for b in blocks:
            assert "--exclude 'backups/'" in b
            assert "--exclude 'predictions_archive/'" in b


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
