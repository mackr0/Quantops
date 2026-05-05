"""Master kill switch tests.

Doomsday gate: a single boolean flag that blocks every new trade entry
across every profile. Auto-flipped by the per-cycle book-loss-floor
task when book-wide day-of P&L breaches a configurable floor.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture
def tmp_master_db(tmp_path, monkeypatch):
    db = str(tmp_path / "master.db")
    import config
    monkeypatch.setattr(config, "DB_PATH", db)
    return db


def test_default_is_inactive(tmp_master_db):
    from kill_switch import is_active
    enabled, reason = is_active(db_path=tmp_master_db)
    assert enabled is False
    assert reason == ""


def test_activate_flips_on(tmp_master_db):
    from kill_switch import activate, is_active
    activate("manual test", set_by="tester", db_path=tmp_master_db)
    enabled, reason = is_active(db_path=tmp_master_db)
    assert enabled is True
    assert reason == "manual test"


def test_deactivate_flips_off(tmp_master_db):
    from kill_switch import activate, deactivate, is_active
    activate("test", set_by="tester", db_path=tmp_master_db)
    deactivate(set_by="tester", db_path=tmp_master_db)
    enabled, _ = is_active(db_path=tmp_master_db)
    assert enabled is False


def test_history_records_transitions(tmp_master_db):
    from kill_switch import activate, deactivate, get_history
    activate("first", set_by="a", db_path=tmp_master_db)
    deactivate(set_by="a", db_path=tmp_master_db)
    activate("second", set_by="b", db_path=tmp_master_db)
    rows = get_history(limit=10, db_path=tmp_master_db)
    actions = [r["action"] for r in rows]
    assert "activate" in actions
    assert "deactivate" in actions
    assert len(rows) == 3


def test_idempotent_activate_doesnt_spam_history(tmp_master_db):
    from kill_switch import activate, get_history
    activate("same", set_by="x", db_path=tmp_master_db)
    activate("same", set_by="x", db_path=tmp_master_db)
    activate("same", set_by="x", db_path=tmp_master_db)
    rows = get_history(limit=10, db_path=tmp_master_db)
    assert len(rows) == 1


def test_reason_change_creates_new_history_row(tmp_master_db):
    from kill_switch import activate, get_history
    activate("first reason", set_by="x", db_path=tmp_master_db)
    activate("different reason", set_by="x", db_path=tmp_master_db)
    rows = get_history(limit=10, db_path=tmp_master_db)
    assert len(rows) == 2


def _seed_profile_db(path, baseline_equity, today_equity):
    """Helper: create a profile DB with two daily_snapshot rows."""
    from journal import init_db
    init_db(path)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO daily_snapshots (date, equity, cash, "
        "portfolio_value, num_positions) "
        "VALUES (date('now', '-1 day'), ?, 0, 0, 0)",
        (baseline_equity,),
    )
    conn.execute(
        "INSERT INTO daily_snapshots (date, equity, cash, "
        "portfolio_value, num_positions) "
        "VALUES (date('now'), ?, 0, 0, 0)",
        (today_equity,),
    )
    conn.commit()
    conn.close()


def test_book_pnl_pct_aggregates_across_profiles(tmp_path):
    from kill_switch import compute_book_day_pnl_pct
    p1 = str(tmp_path / "p1.db")
    p2 = str(tmp_path / "p2.db")
    # Profile 1: $100K → $90K (down 10%)
    # Profile 2: $200K → $180K (down 10%)
    # Combined: $300K → $270K (down 10%)
    _seed_profile_db(p1, 100000, 90000)
    _seed_profile_db(p2, 200000, 180000)
    pnl = compute_book_day_pnl_pct([p1, p2])
    assert abs(pnl - (-10.0)) < 0.01


def test_loss_floor_activates_on_breach(tmp_path, tmp_master_db):
    from kill_switch import check_and_activate_on_loss_floor, is_active
    p1 = str(tmp_path / "p1.db")
    _seed_profile_db(p1, 100000, 88000)  # -12%
    pnl = check_and_activate_on_loss_floor(
        [p1], floor_pct=-8.0, db_path=tmp_master_db,
    )
    assert pnl is not None
    assert pnl < -8.0
    enabled, reason = is_active(db_path=tmp_master_db)
    assert enabled is True
    assert "-12" in reason or "breached" in reason


def test_loss_floor_does_not_activate_on_small_loss(tmp_path, tmp_master_db):
    from kill_switch import check_and_activate_on_loss_floor, is_active
    p1 = str(tmp_path / "p1.db")
    _seed_profile_db(p1, 100000, 95000)  # -5%, above floor
    check_and_activate_on_loss_floor([p1], floor_pct=-8.0, db_path=tmp_master_db)
    enabled, _ = is_active(db_path=tmp_master_db)
    assert enabled is False


def test_loss_floor_returns_none_when_no_baseline(tmp_path, tmp_master_db):
    """If no profile has a baseline snapshot, can't compute — return
    None and don't activate (defense against bad-data false positives)."""
    from kill_switch import check_and_activate_on_loss_floor, is_active
    pnl = check_and_activate_on_loss_floor([], db_path=tmp_master_db)
    assert pnl is None
    enabled, _ = is_active(db_path=tmp_master_db)
    assert enabled is False
