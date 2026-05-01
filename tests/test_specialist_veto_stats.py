"""Tests for journal.get_specialist_veto_stats — the data behind the
veto-rate panel.

Real bug surfaced when this was first wired: only pattern_recognizer
and sentiment_narrative emit VETO across all 10 prod profiles, but
neither has VETO authority — so those vetoes are silent no-ops. The
panel needs to clearly distinguish 'effective' (authority granted) from
'claimed' (verdict written but no enforcement).
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture
def tmp_db():
    from journal import init_db
    from specialist_calibration import init_calibration_db
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    init_calibration_db(path)  # creates specialist_outcomes table
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


def _seed(db_path, name, verdict, raw_conf=70, prediction_id=None):
    """Insert one specialist_outcomes row."""
    import sqlite3
    if prediction_id is None:
        # Each row needs a unique prediction_id (UNIQUE on (pid, name))
        prediction_id = abs(hash((name, verdict, raw_conf, datetime.utcnow().isoformat()))) % 1000000000
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO specialist_outcomes "
        "(prediction_id, specialist_name, verdict, raw_confidence) "
        "VALUES (?, ?, ?, ?)",
        (prediction_id, name, verdict, raw_conf),
    )
    conn.commit()
    conn.close()


class TestVetoStats:
    def test_empty_dbs_returns_empty(self, tmp_db):
        from journal import get_specialist_veto_stats
        stats = get_specialist_veto_stats([tmp_db], days=7)
        assert stats["total_vetoes_claimed"] == 0
        assert stats["total_vetoes_effective"] == 0
        assert stats["by_specialist"] == []

    def test_authorized_specialist_veto_counted_as_effective(self, tmp_db):
        for i in range(5):
            _seed(tmp_db, "risk_assessor", "VETO", prediction_id=i + 1)
        for i in range(10):
            _seed(tmp_db, "risk_assessor", "HOLD", prediction_id=i + 100)
        from journal import get_specialist_veto_stats
        stats = get_specialist_veto_stats([tmp_db], days=7)
        assert stats["total_vetoes_claimed"] == 5
        assert stats["total_vetoes_effective"] == 5
        spec = stats["by_specialist"][0]
        assert spec["name"] == "risk_assessor"
        assert spec["total"] == 15
        assert spec["vetoes"] == 5
        assert spec["veto_rate_pct"] == pytest.approx(33.3, abs=0.1)
        assert spec["has_authority"] is True
        assert spec["effective_vetoes"] == 5

    def test_unauthorized_specialist_veto_not_effective(self, tmp_db):
        """The exact bug seen on prod: pattern_recognizer emits VETO
        but lacks authority. The panel must show effective_vetoes=0
        even though vetoes>0 — these are silent no-ops."""
        for i in range(7):
            _seed(tmp_db, "pattern_recognizer", "VETO", prediction_id=i + 1)
        for i in range(20):
            _seed(tmp_db, "pattern_recognizer", "BUY", prediction_id=i + 100)
        from journal import get_specialist_veto_stats
        stats = get_specialist_veto_stats([tmp_db], days=7)
        assert stats["total_vetoes_claimed"] == 7
        assert stats["total_vetoes_effective"] == 0  # silent no-op
        spec = stats["by_specialist"][0]
        assert spec["has_authority"] is False
        assert spec["vetoes"] == 7
        assert spec["effective_vetoes"] == 0

    def test_adversarial_reviewer_has_authority(self, tmp_db):
        """Item 5b: the adversarial_reviewer was added with VETO
        authority. Make sure get_specialist_veto_stats reflects that."""
        for i in range(3):
            _seed(tmp_db, "adversarial_reviewer", "VETO", prediction_id=i + 1)
        from journal import get_specialist_veto_stats
        stats = get_specialist_veto_stats([tmp_db], days=7)
        spec = stats["by_specialist"][0]
        assert spec["name"] == "adversarial_reviewer"
        assert spec["has_authority"] is True
        assert spec["effective_vetoes"] == 3

    def test_aggregates_across_multiple_profiles(self, tmp_db):
        """Per-DB counts add up; the panel renders one row per
        specialist across the full multi-profile set."""
        # Spin up a 2nd DB for this test
        from journal import init_db, get_specialist_veto_stats
        from specialist_calibration import init_calibration_db
        fd, path2 = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        init_db(path2)
        init_calibration_db(path2)
        try:
            for i in range(2):
                _seed(tmp_db, "risk_assessor", "VETO", prediction_id=i + 1)
            for i in range(3):
                _seed(path2, "risk_assessor", "VETO", prediction_id=i + 1)
            stats = get_specialist_veto_stats([tmp_db, path2], days=7)
            assert stats["total_vetoes_effective"] == 5
        finally:
            os.unlink(path2)

    def test_sorted_by_veto_count_descending(self, tmp_db):
        """Most-vetoing specialist appears first — surfaces the noisy
        no-op cases prominently when they're the loudest."""
        for i in range(15):
            _seed(tmp_db, "pattern_recognizer", "VETO", prediction_id=i + 1)
        for i in range(2):
            _seed(tmp_db, "risk_assessor", "VETO", prediction_id=i + 100)
        from journal import get_specialist_veto_stats
        stats = get_specialist_veto_stats([tmp_db], days=7)
        names = [s["name"] for s in stats["by_specialist"]]
        assert names == ["pattern_recognizer", "risk_assessor"]

    def test_handles_missing_db_gracefully(self):
        from journal import get_specialist_veto_stats
        stats = get_specialist_veto_stats(["/nonexistent.db"], days=7)
        # Doesn't crash; returns empty
        assert stats["total_vetoes_claimed"] == 0
        assert stats["by_specialist"] == []
