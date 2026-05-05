"""AI consistency floor tests."""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture(autouse=True)
def _reset_state():
    from ai_consistency_floor import reset_state
    reset_state()
    yield
    reset_state()


def _setup_predictions(path, outcomes, signal="BUY"):
    """outcomes = list of 'win'/'loss' strings."""
    from ai_tracker import init_tracker_db
    init_tracker_db(path)
    conn = sqlite3.connect(path)
    for outcome in outcomes:
        conn.execute(
            "INSERT INTO ai_predictions "
            "(timestamp, symbol, predicted_signal, confidence, "
            "price_at_prediction, status, actual_outcome, "
            "actual_return_pct, days_held) "
            "VALUES (datetime('now'), 'X', ?, 50, 100.0, "
            "'resolved', ?, ?, 5)",
            (signal, outcome, 1.0 if outcome == "win" else -1.0),
        )
    conn.commit()
    conn.close()


def test_no_history_returns_none(tmp_path):
    from ai_consistency_floor import recent_win_rate
    from ai_tracker import init_tracker_db
    p = str(tmp_path / "p.db")
    init_tracker_db(p)
    assert recent_win_rate(p) is None


def test_insufficient_history_returns_none(tmp_path):
    from ai_consistency_floor import recent_win_rate
    p = str(tmp_path / "p.db")
    _setup_predictions(p, ["win"] * 5)  # <10 -> not enough
    assert recent_win_rate(p) is None


def test_high_win_rate(tmp_path):
    from ai_consistency_floor import recent_win_rate
    p = str(tmp_path / "p.db")
    _setup_predictions(p, ["win"] * 70 + ["loss"] * 30)
    info = recent_win_rate(p)
    assert info["n_resolved"] == 100
    assert info["n_wins"] == 70
    assert info["win_rate_pct"] == 70.0


def test_holds_excluded(tmp_path):
    from ai_consistency_floor import recent_win_rate
    p = str(tmp_path / "p.db")
    # 20 directional, 80 holds
    _setup_predictions(p, ["win"] * 10 + ["loss"] * 10, signal="BUY")
    _setup_predictions(p, ["win"] * 80, signal="HOLD")
    info = recent_win_rate(p)
    assert info["n_resolved"] == 20  # holds filtered
    assert info["win_rate_pct"] == 50.0


def test_breach_increments_consecutive_no_alert_yet(tmp_path):
    from ai_consistency_floor import check_floor
    p = str(tmp_path / "p.db")
    _setup_predictions(p, ["loss"] * 80 + ["win"] * 20)
    out = check_floor(p, "P", floor_pct=30.0, consecutive_required=5)
    assert out["breached"] is True
    assert out["consecutive"] == 1
    assert out["alert_now"] is False


def test_alert_fires_at_threshold(tmp_path):
    from ai_consistency_floor import check_floor
    p = str(tmp_path / "p.db")
    _setup_predictions(p, ["loss"] * 80 + ["win"] * 20)
    for _ in range(4):
        check_floor(p, "P", floor_pct=30.0, consecutive_required=5)
    out = check_floor(p, "P", floor_pct=30.0, consecutive_required=5)
    assert out["consecutive"] == 5
    assert out["alert_now"] is True


def test_alert_does_not_repeat_after_threshold(tmp_path):
    """alert_now is True only ON the cycle that meets threshold."""
    from ai_consistency_floor import check_floor
    p = str(tmp_path / "p.db")
    _setup_predictions(p, ["loss"] * 80 + ["win"] * 20)
    for _ in range(5):
        check_floor(p, "P", floor_pct=30.0, consecutive_required=5)
    out = check_floor(p, "P", floor_pct=30.0, consecutive_required=5)
    # Already alerted at consecutive=5; consecutive=6 should NOT alert
    assert out["consecutive"] == 6
    assert out["alert_now"] is False


def test_recovery_resets_counter(tmp_path):
    from ai_consistency_floor import check_floor
    p = str(tmp_path / "p.db")
    _setup_predictions(p, ["loss"] * 80 + ["win"] * 20)
    for _ in range(3):
        out = check_floor(p, "P", floor_pct=30.0, consecutive_required=5)
        assert out["breached"] is True
    # Now flip the data — high win rate
    conn = sqlite3.connect(p)
    conn.execute("UPDATE ai_predictions SET actual_outcome = 'win'")
    conn.commit()
    conn.close()
    out = check_floor(p, "P", floor_pct=30.0, consecutive_required=5)
    assert out["breached"] is False
    assert out["consecutive"] == 0
