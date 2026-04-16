"""Tests for the self-tuning visibility improvements (2026-04-15).

User complaint: "I never see any evidence of auto-tuning." The tuner
does run daily, but when it has insufficient data or no changes to
make, it exited silently — no activity row, no dashboard signal.

Fix: `describe_tuning_state(ctx)` returns a human-readable status
struct the scheduler logs every run + the performance page shows
as a "Self-Tuning Status" panel.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from types import SimpleNamespace

import pytest


@pytest.fixture
def ctx_with_fresh_db(monkeypatch):
    tmpdir = tempfile.mkdtemp()
    monkeypatch.chdir(tmpdir)
    path = os.path.join(tmpdir, "t.db")
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE ai_predictions (
            id INTEGER PRIMARY KEY,
            symbol TEXT,
            status TEXT DEFAULT 'pending'
        )
    """)
    conn.commit()
    conn.close()
    ctx = SimpleNamespace(
        profile_id=1, user_id=1, db_path=path,
        enable_self_tuning=True, display_name="Test",
        segment="small",
    )
    return ctx, path


class TestDescribeTuningState:
    def test_returns_can_tune_false_when_disabled(self, ctx_with_fresh_db):
        from self_tuning import describe_tuning_state
        ctx, _ = ctx_with_fresh_db
        ctx.enable_self_tuning = False
        state = describe_tuning_state(ctx)
        assert state["can_tune"] is False
        assert "disabled" in state["message"].lower()

    def test_returns_resolved_count(self, ctx_with_fresh_db):
        from self_tuning import describe_tuning_state
        ctx, path = ctx_with_fresh_db
        conn = sqlite3.connect(path)
        for _ in range(5):
            conn.execute("INSERT INTO ai_predictions (symbol, status) VALUES ('A','resolved')")
        for _ in range(10):
            conn.execute("INSERT INTO ai_predictions (symbol, status) VALUES ('A','pending')")
        conn.commit()
        conn.close()
        state = describe_tuning_state(ctx)
        assert state["resolved"] == 5
        assert state["required"] == 20
        assert state["can_tune"] is False
        assert "5/20" in state["message"] or "5 / 20" in state["message"]

    def test_can_tune_when_threshold_met(self, ctx_with_fresh_db):
        from self_tuning import describe_tuning_state
        ctx, path = ctx_with_fresh_db
        conn = sqlite3.connect(path)
        for _ in range(25):
            conn.execute("INSERT INTO ai_predictions (symbol, status) VALUES ('A','resolved')")
        conn.commit()
        conn.close()
        state = describe_tuning_state(ctx)
        assert state["resolved"] == 25
        assert state["can_tune"] is True
        assert "25" in state["message"]

    def test_returns_nice_message_when_no_table(self, monkeypatch, tmp_path):
        """Brand new profile without an ai_predictions table yet —
        shouldn't crash, should return a helpful message."""
        from self_tuning import describe_tuning_state
        monkeypatch.chdir(tmp_path)
        empty_db = tmp_path / "empty.db"
        sqlite3.connect(empty_db).close()  # empty DB, no tables
        ctx = SimpleNamespace(
            profile_id=1, user_id=1, db_path=str(empty_db),
            enable_self_tuning=True, display_name="t", segment="s",
        )
        state = describe_tuning_state(ctx)
        assert state["can_tune"] is False
        assert state["resolved"] == 0

    def test_message_mentions_waiting_when_below_threshold(self, ctx_with_fresh_db):
        from self_tuning import describe_tuning_state
        ctx, _ = ctx_with_fresh_db
        state = describe_tuning_state(ctx)
        # Should communicate that it's a waiting state, not a failure
        msg = state["message"].lower()
        assert "wait" in msg or "more" in msg or "ready" in msg
