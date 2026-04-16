"""Tests for Phase 10 — cross-asset crisis detection.

Covers threshold classification, signal combinations, state transitions,
event emission on transition, and the tick idempotence when level is
unchanged.
"""

from __future__ import annotations

import json
import sqlite3
import pytest


# ---------------------------------------------------------------------------
# Classification logic (no network)
# ---------------------------------------------------------------------------

class TestClassifyLevel:
    def test_normal_when_no_signals(self):
        from crisis_detector import _classify_level, NORMAL
        assert _classify_level([], vix_level=15) == NORMAL

    def test_severe_when_vix_above_severe_threshold(self):
        from crisis_detector import _classify_level, SEVERE
        assert _classify_level([], vix_level=50) == SEVERE

    def test_severe_when_critical_signal_present(self):
        from crisis_detector import _classify_level, SEVERE
        signals = [{"name": "vix_severe", "severity": "critical", "detail": ""}]
        assert _classify_level(signals, vix_level=15) == SEVERE

    def test_crisis_when_vix_in_crisis_range(self):
        from crisis_detector import _classify_level, CRISIS
        assert _classify_level([], vix_level=35) == CRISIS

    def test_crisis_upgrades_to_severe_with_two_high_signals(self):
        from crisis_detector import _classify_level, SEVERE
        signals = [
            {"name": "a", "severity": "high"},
            {"name": "b", "severity": "high"},
        ]
        assert _classify_level(signals, vix_level=35) == SEVERE

    def test_elevated_when_vix_elevated(self):
        from crisis_detector import _classify_level, ELEVATED
        assert _classify_level([], vix_level=25) == ELEVATED

    def test_elevated_upgrades_to_crisis_with_many_signals(self):
        from crisis_detector import _classify_level, CRISIS
        signals = [
            {"name": "a", "severity": "medium"},
            {"name": "b", "severity": "high"},
            {"name": "c", "severity": "medium"},
            {"name": "d", "severity": "high"},
        ]
        assert _classify_level(signals, vix_level=25) == CRISIS

    def test_normal_vix_escalates_with_five_plus_signals(self):
        from crisis_detector import _classify_level, SEVERE, CRISIS, ELEVATED
        three = [{"name": str(i), "severity": "medium"} for i in range(3)]
        assert _classify_level(three, vix_level=15) == CRISIS
        one = [{"name": "x", "severity": "medium"}]
        assert _classify_level(one, vix_level=15) == ELEVATED
        five = [{"name": str(i), "severity": "medium"} for i in range(5)]
        assert _classify_level(five, vix_level=15) == SEVERE


# ---------------------------------------------------------------------------
# Size multiplier mapping
# ---------------------------------------------------------------------------

class TestSizeMultipliers:
    def test_multipliers_match_levels(self):
        from crisis_detector import SIZE_MULTIPLIERS
        assert SIZE_MULTIPLIERS["normal"] == 1.0
        assert 0 < SIZE_MULTIPLIERS["elevated"] < 1.0
        assert SIZE_MULTIPLIERS["crisis"] == 0.0
        assert SIZE_MULTIPLIERS["severe"] == 0.0


# ---------------------------------------------------------------------------
# State persistence and transitions
# ---------------------------------------------------------------------------

class TestStatePersistence:
    def test_initial_level_is_normal(self, tmp_profile_db):
        from crisis_state import get_current_level
        cur = get_current_level(tmp_profile_db)
        assert cur["level"] == "normal"
        assert cur["size_multiplier"] == 1.0

    def test_transition_writes_history_row(self, tmp_profile_db, monkeypatch):
        import crisis_state

        def fake_detect(db_path=None):
            return {
                "level": "crisis",
                "signals": [{"name": "vix_crisis", "severity": "high",
                             "detail": "VIX spike"}],
                "readings": {"vix": 35.0},
                "size_multiplier": 0.0,
            }

        monkeypatch.setattr(crisis_state, "detect_crisis_state", fake_detect)
        result = crisis_state.run_crisis_tick(tmp_profile_db)
        assert result["changed"] is True
        assert result["level"] == "crisis"
        assert result["prior_level"] == "normal"

        conn = sqlite3.connect(tmp_profile_db)
        row = conn.execute(
            "SELECT from_level, to_level, size_multiplier "
            "FROM crisis_state_history ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        assert row == ("normal", "crisis", 0.0)

    def test_unchanged_level_does_not_write_history(
        self, tmp_profile_db, monkeypatch
    ):
        import crisis_state

        def fake_detect(db_path=None):
            return {"level": "normal", "signals": [],
                    "readings": {}, "size_multiplier": 1.0}

        monkeypatch.setattr(crisis_state, "detect_crisis_state", fake_detect)
        crisis_state.run_crisis_tick(tmp_profile_db)
        crisis_state.run_crisis_tick(tmp_profile_db)

        conn = sqlite3.connect(tmp_profile_db)
        n = conn.execute(
            "SELECT COUNT(*) FROM crisis_state_history"
        ).fetchone()[0]
        conn.close()
        assert n == 0  # no transitions written for unchanged state

    def test_transition_emits_event(self, tmp_profile_db, monkeypatch):
        import crisis_state

        def fake_detect(db_path=None):
            return {
                "level": "severe",
                "signals": [{"name": "critical", "severity": "critical",
                             "detail": ""}],
                "readings": {},
                "size_multiplier": 0.0,
            }

        monkeypatch.setattr(crisis_state, "detect_crisis_state", fake_detect)
        crisis_state.run_crisis_tick(tmp_profile_db)

        conn = sqlite3.connect(tmp_profile_db)
        row = conn.execute(
            "SELECT type, severity, payload_json FROM events "
            "WHERE type='crisis_state_change'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[1] == "critical"
        payload = json.loads(row[2])
        assert payload["to"] == "severe"
        assert payload["from"] == "normal"

    def test_downgrade_uses_info_severity(self, tmp_profile_db, monkeypatch):
        """Recovery (severe → normal) should emit as info, not critical."""
        import crisis_state

        # First: climb to severe
        def detect_severe(db_path=None):
            return {"level": "severe",
                    "signals": [{"name": "c", "severity": "critical", "detail": ""}],
                    "readings": {}, "size_multiplier": 0.0}
        monkeypatch.setattr(crisis_state, "detect_crisis_state", detect_severe)
        crisis_state.run_crisis_tick(tmp_profile_db)

        # Then: recover
        def detect_normal(db_path=None):
            return {"level": "normal", "signals": [],
                    "readings": {}, "size_multiplier": 1.0}
        monkeypatch.setattr(crisis_state, "detect_crisis_state", detect_normal)
        crisis_state.run_crisis_tick(tmp_profile_db)

        conn = sqlite3.connect(tmp_profile_db)
        rows = conn.execute(
            "SELECT severity, payload_json FROM events "
            "WHERE type='crisis_state_change' ORDER BY id"
        ).fetchall()
        conn.close()
        assert len(rows) == 2
        assert rows[0][0] == "critical"   # upgrade to severe
        assert rows[1][0] == "info"       # downgrade to normal

    def test_history_returns_rows_newest_first(self, tmp_profile_db, monkeypatch):
        import crisis_state

        for level in ("elevated", "crisis", "normal"):
            def fake(db_path=None, _l=level):
                return {"level": _l, "signals": [{"name": "x", "severity": "medium"}],
                        "readings": {}, "size_multiplier": 0.5 if _l == "elevated" else (0.0 if _l == "crisis" else 1.0)}
            monkeypatch.setattr(crisis_state, "detect_crisis_state", fake)
            crisis_state.run_crisis_tick(tmp_profile_db)

        hist = crisis_state.history(tmp_profile_db)
        assert [h["to_level"] for h in hist] == ["normal", "crisis", "elevated"]


# ---------------------------------------------------------------------------
# Get current level returns latest row
# ---------------------------------------------------------------------------

class TestGetCurrentLevel:
    def test_returns_latest_row(self, tmp_profile_db, monkeypatch):
        import crisis_state

        def fake(db_path=None):
            return {"level": "elevated",
                    "signals": [{"name": "s", "severity": "medium"}],
                    "readings": {"vix": 24.5}, "size_multiplier": 0.5}
        monkeypatch.setattr(crisis_state, "detect_crisis_state", fake)
        crisis_state.run_crisis_tick(tmp_profile_db)

        cur = crisis_state.get_current_level(tmp_profile_db)
        assert cur["level"] == "elevated"
        assert cur["size_multiplier"] == 0.5
        assert cur["readings"]["vix"] == 24.5


# ---------------------------------------------------------------------------
# Level rank ordering
# ---------------------------------------------------------------------------

class TestLevelRank:
    def test_rank_is_monotonic(self):
        from crisis_detector import LEVEL_RANK, NORMAL, ELEVATED, CRISIS, SEVERE
        assert (LEVEL_RANK[NORMAL] < LEVEL_RANK[ELEVATED]
                < LEVEL_RANK[CRISIS] < LEVEL_RANK[SEVERE])
