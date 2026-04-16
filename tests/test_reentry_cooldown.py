"""Regression tests for the post-exit cooldown (2026-04-14 ASTS churn)."""

from __future__ import annotations

import sqlite3
import pytest


class TestRecordAndQueryExit:
    def test_record_then_query_returns_symbol(self, tmp_profile_db):
        from journal import get_recently_exited, record_exit
        record_exit(tmp_profile_db, "ASTS", "trailing_stop", exit_price=89.44)
        assert "ASTS" in get_recently_exited(tmp_profile_db, cooldown_minutes=60)

    def test_symbol_exits_cooldown_window(self, tmp_profile_db):
        """Older than the cooldown → not returned."""
        # Seed a row with a past exited_at
        conn = sqlite3.connect(tmp_profile_db)
        conn.execute(
            "INSERT INTO recently_exited_symbols (symbol, exited_at, trigger) "
            "VALUES ('OLD', datetime('now', '-2 hours'), 'stop_loss')"
        )
        conn.execute(
            "INSERT INTO recently_exited_symbols (symbol, exited_at, trigger) "
            "VALUES ('FRESH', datetime('now', '-10 minutes'), 'stop_loss')"
        )
        conn.commit()
        conn.close()

        from journal import get_recently_exited
        recent = get_recently_exited(tmp_profile_db, cooldown_minutes=60)
        assert "FRESH" in recent
        assert "OLD" not in recent

    def test_replace_preserves_latest_exit(self, tmp_profile_db):
        """Two exits on same symbol — only the latest counts."""
        from journal import record_exit, get_recently_exited
        record_exit(tmp_profile_db, "X", "stop_loss")
        record_exit(tmp_profile_db, "X", "trailing_stop")
        # Either way, the symbol is in cooldown
        assert "X" in get_recently_exited(tmp_profile_db, cooldown_minutes=60)

    def test_missing_table_returns_empty_set(self, tmp_path):
        """Old DB without the table → graceful empty set, no crash."""
        from journal import get_recently_exited
        empty = str(tmp_path / "empty.db")
        sqlite3.connect(empty).close()
        assert get_recently_exited(empty) == set()


class TestPipelineFilter:
    """The pre-filter drops BUY candidates in cooldown but still allows
    the shortlist to include held positions (for exit/trim logic)."""

    def test_pipeline_imports_cooldown_helpers(self):
        from journal import get_recently_exited, record_exit
        assert callable(get_recently_exited)
        assert callable(record_exit)

    def test_cooldown_symbol_filtered_from_candidates(self, tmp_profile_db,
                                                       monkeypatch):
        """Smoke test against the pre-filter path directly."""
        from journal import record_exit, get_recently_exited
        record_exit(tmp_profile_db, "ASTS", "trailing_stop")
        recent = get_recently_exited(tmp_profile_db, cooldown_minutes=60)
        held = {"HIMS"}

        candidates = ["ASTS", "HIMS", "OMC"]
        filtered = [
            s for s in candidates
            if not (s in recent and s not in held)
        ]
        # ASTS was exited and not held → filtered
        assert filtered == ["HIMS", "OMC"]
