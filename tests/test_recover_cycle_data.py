"""Tests for recover_cycle_data — protects against future re-occurrence
of the bug where deploys wiped runtime cycle_data files."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta

import pytest


def _seed_predictions(db_path: str, count: int = 5,
                      symbol_prefix: str = "AAPL") -> None:
    """Insert N ai_predictions rows with timestamps in the last hour."""
    from journal import init_db
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    base = datetime.utcnow() - timedelta(minutes=30)
    for i in range(count):
        ts = (base + timedelta(minutes=i)).isoformat()
        conn.execute(
            """INSERT INTO ai_predictions
                 (timestamp, symbol, predicted_signal, confidence,
                  reasoning, price_at_prediction, status)
               VALUES (?, ?, ?, ?, ?, ?, 'pending')""",
            (ts, f"{symbol_prefix}{i}",
             "BUY" if i % 2 == 0 else "HOLD",
             50 + i,
             f"test reasoning {i}",
             100.0 + i),
        )
    conn.commit()
    conn.close()


class TestReconstruct:
    def test_creates_valid_cycle_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _seed_predictions(str(tmp_path / "quantopsai_profile_99.db"), count=5)

        from recover_cycle_data import reconstruct
        ok = reconstruct(99, "Test Profile")
        assert ok is True

        out = tmp_path / "cycle_data_99.json"
        assert out.exists()
        d = json.loads(out.read_text())
        assert d["profile_id"] == 99
        assert d["_reconstructed"] is True
        assert "Reconstructed from prediction history" in d["ai_reasoning"]
        # 3 of 5 are BUY (i=0,2,4) → trades
        assert len(d["trades_selected"]) == 3
        assert len(d["shortlist"]) == 5

    def test_skip_when_recent_file_exists(self, tmp_path, monkeypatch):
        """Defends against re-introducing the bug where we overwrote a
        live cycle file with a stale reconstruction."""
        monkeypatch.chdir(tmp_path)
        _seed_predictions(str(tmp_path / "quantopsai_profile_99.db"), count=5)
        # Plant a "live" file with rich data
        live = {"ai_reasoning": "live data — DO NOT OVERWRITE",
                "timestamp": time.time(),
                "ensemble": {"enabled": True, "cost_calls": 4}}
        live_path = tmp_path / "cycle_data_99.json"
        live_path.write_text(json.dumps(live))

        from recover_cycle_data import reconstruct
        ok = reconstruct(99, "Test")
        assert ok is False

        # Live file was preserved
        d = json.loads(live_path.read_text())
        assert "DO NOT OVERWRITE" in d["ai_reasoning"]
        assert d["ensemble"]["cost_calls"] == 4

    def test_force_overrides_freshness_check(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _seed_predictions(str(tmp_path / "quantopsai_profile_99.db"), count=3)
        live_path = tmp_path / "cycle_data_99.json"
        live_path.write_text(json.dumps({"ai_reasoning": "old"}))

        from recover_cycle_data import reconstruct
        ok = reconstruct(99, "Test", force=True)
        assert ok is True
        d = json.loads(live_path.read_text())
        assert "Reconstructed" in d["ai_reasoning"]

    def test_skip_when_db_missing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from recover_cycle_data import reconstruct
        ok = reconstruct(999)
        assert ok is False

    def test_skip_when_no_predictions(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # Create empty DB with the table but no rows
        from journal import init_db
        init_db(str(tmp_path / "quantopsai_profile_99.db"))
        from recover_cycle_data import reconstruct
        ok = reconstruct(99)
        assert ok is False


class TestSyncShExclusions:
    """Guardrail against reintroducing the cycle_data wipe bug."""

    def test_sync_excludes_runtime_artifacts(self):
        with open("sync.sh") as f:
            content = f.read()
        # Both runtime artifacts must be excluded
        assert "cycle_data_*.json" in content, (
            "sync.sh missing cycle_data exclusion — deploys will wipe "
            "dashboard state again. See test_recover_cycle_data.py."
        )
        assert "scheduler_status.json" in content
        # Database files must always be excluded
        assert "*.db" in content
        assert "*.db-wal" in content
