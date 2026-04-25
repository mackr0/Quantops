"""Tests for the AI rolling win-rate timeseries + SVG renderer."""

import sqlite3
from datetime import date, datetime, timedelta

from ai_tracker import compute_rolling_win_rate, init_tracker_db
from metrics import render_win_rate_svg


def _seed(db_path, rows):
    """rows = list of (resolved_at_iso, outcome)."""
    init_tracker_db(db_path)
    conn = sqlite3.connect(db_path)
    for resolved_at, outcome in rows:
        conn.execute(
            "INSERT INTO ai_predictions "
            "(symbol, predicted_signal, price_at_prediction, status, "
            " actual_outcome, resolved_at) "
            "VALUES (?, ?, ?, 'resolved', ?, ?)",
            ("X", "BUY", 100.0, outcome, resolved_at),
        )
    conn.commit()
    conn.close()


class TestComputeRollingWinRate:
    def test_no_resolved_predictions_returns_all_none(self, tmp_db):
        init_tracker_db(tmp_db)
        series = compute_rolling_win_rate([tmp_db], window_days=7, lookback_days=10)
        assert len(series) == 11  # lookback_days + 1 inclusive
        assert all(p["win_rate"] is None for p in series)
        assert all(p["n"] == 0 for p in series)

    def test_pure_winning_streak_in_window(self, tmp_db):
        today = date.today()
        rows = [((today - timedelta(days=i)).isoformat() + "T12:00:00", "win")
                for i in range(5)]
        _seed(tmp_db, rows)
        series = compute_rolling_win_rate([tmp_db], window_days=7, lookback_days=10)
        # Today's window covers all 5 wins.
        last = series[-1]
        assert last["date"] == today.isoformat()
        assert last["n"] == 5
        assert last["win_rate"] == 100.0

    def test_mixed_outcomes_compute_correct_pct(self, tmp_db):
        today = date.today()
        rows = (
            [((today - timedelta(days=i)).isoformat() + "T12:00:00", "win")
             for i in range(3)]
            + [((today - timedelta(days=i)).isoformat() + "T12:00:00", "loss")
               for i in range(3)]
        )
        _seed(tmp_db, rows)
        series = compute_rolling_win_rate([tmp_db], window_days=7, lookback_days=5)
        assert series[-1]["n"] == 6
        assert series[-1]["win_rate"] == 50.0

    def test_neutral_outcomes_excluded(self, tmp_db):
        today = date.today()
        rows = [
            (today.isoformat() + "T12:00:00", "win"),
            (today.isoformat() + "T12:00:00", "neutral"),
            (today.isoformat() + "T12:00:00", "neutral"),
        ]
        _seed(tmp_db, rows)
        series = compute_rolling_win_rate([tmp_db], window_days=7, lookback_days=5)
        # Only the win counts.
        assert series[-1]["n"] == 1
        assert series[-1]["win_rate"] == 100.0

    def test_window_excludes_predictions_outside_range(self, tmp_db):
        today = date.today()
        old = today - timedelta(days=20)
        rows = [(old.isoformat() + "T12:00:00", "loss")]
        _seed(tmp_db, rows)
        series = compute_rolling_win_rate([tmp_db], window_days=7, lookback_days=10)
        # No resolutions land in any of the last-10-days windows.
        assert all(p["n"] == 0 for p in series)

    def test_aggregates_across_multiple_dbs(self, tmp_path):
        today = date.today()
        db_a = str(tmp_path / "a.db")
        db_b = str(tmp_path / "b.db")
        _seed(db_a, [(today.isoformat() + "T12:00:00", "win")])
        _seed(db_b, [(today.isoformat() + "T12:00:00", "loss")])
        series = compute_rolling_win_rate([db_a, db_b],
                                          window_days=7, lookback_days=3)
        assert series[-1]["n"] == 2
        assert series[-1]["win_rate"] == 50.0


class TestRenderWinRateSvg:
    def test_empty_series_renders_placeholder(self):
        svg = render_win_rate_svg([])
        assert "<svg" in svg
        assert "Need more resolved predictions" in svg

    def test_all_none_series_renders_placeholder(self):
        series = [{"date": "2026-04-01", "win_rate": None, "n": 0}] * 5
        svg = render_win_rate_svg(series)
        assert "Need more resolved predictions" in svg

    def test_renders_polyline_with_data(self):
        series = [
            {"date": "2026-04-01", "win_rate": 40.0, "n": 5},
            {"date": "2026-04-02", "win_rate": 55.0, "n": 6},
            {"date": "2026-04-03", "win_rate": 60.0, "n": 8},
        ]
        svg = render_win_rate_svg(series)
        assert "<polyline" in svg
        assert "2026-04-01" in svg
        assert "2026-04-03" in svg
        # Final point > 50 should pick the green color.
        assert "#00c853" in svg

    def test_below_50_renders_red(self):
        series = [
            {"date": "2026-04-01", "win_rate": 60.0, "n": 5},
            {"date": "2026-04-02", "win_rate": 45.0, "n": 6},
            {"date": "2026-04-03", "win_rate": 30.0, "n": 8},
        ]
        svg = render_win_rate_svg(series)
        assert "#ff1744" in svg

    def test_gap_breaks_into_segments(self):
        # Two valid runs separated by a gap — should yield 2 polylines.
        series = [
            {"date": "2026-04-01", "win_rate": 60.0, "n": 5},
            {"date": "2026-04-02", "win_rate": 55.0, "n": 4},
            {"date": "2026-04-03", "win_rate": None, "n": 0},
            {"date": "2026-04-04", "win_rate": None, "n": 0},
            {"date": "2026-04-05", "win_rate": 50.0, "n": 3},
            {"date": "2026-04-06", "win_rate": 52.0, "n": 4},
        ]
        svg = render_win_rate_svg(series)
        assert svg.count("<polyline") == 2
