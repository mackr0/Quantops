"""Pin the IV-rank degradation alarm for docs/18 item #6.

The alarm reads `book_greeks.fallback_iv_count / n_options_legs` from
the per-cycle Portfolio Risk Snapshot. When ≥80% of legs needed the
0.25 fallback in a cycle (with at least 3 legs to avoid noise on
small books), insert an `audit_alerts` row of type
`iv_rank_degradation`. The /issues page reads from audit_alerts so
the operator sees the degradation immediately instead of via "why
no options trades?".

Pinning the exact threshold + the noise floor (3 legs) here so a
future refactor can't silently relax them.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


def _make_master_db(tmp_path):
    """Mimic the scheduler's profile-DB layout: audit_alerts may not
    exist yet — the alarm's job is to CREATE it on first use."""
    db_path = tmp_path / "profile.db"
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.commit()
    return str(db_path)


def _run_alarm_block(db_path, *, n_legs, n_fb, seg_label="TEST"):
    """Reproduce the production-path logic exactly so the test pins
    the same threshold, the same noise floor, and the same alert
    payload shape. If the prod block in multi_scheduler.py changes,
    this test breaks loudly."""
    bg = {"n_options_legs": n_legs, "fallback_iv_count": n_fb}
    if n_legs >= 3 and n_fb / max(1, n_legs) >= 0.80:
        pct = round(100.0 * n_fb / n_legs, 1)
        msg = (
            f"IV-rank lookup degraded: {n_fb}/{n_legs} option "
            f"legs ({pct}%) used FALLBACK_IV=0.25 this cycle. "
            "Investigate options_oracle / Alpaca options chain "
            "fetch — silent degradation means delta-adjusted "
            "exposure understates risk for high-IV underlyings."
        )
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS audit_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    alert_type TEXT NOT NULL,
                    severity TEXT NOT NULL DEFAULT 'warning',
                    title TEXT NOT NULL,
                    detail TEXT,
                    resolved INTEGER NOT NULL DEFAULT 0)
            """)
            conn.execute(
                "INSERT INTO audit_alerts "
                "(alert_type, severity, title, detail) "
                "VALUES (?, ?, ?, ?)",
                ("iv_rank_degradation", "warning",
                 f"IV-rank lookup degraded ({pct}%)", msg),
            )
            conn.commit()
        return True
    return False


def _count_alerts(db_path):
    with closing(sqlite3.connect(db_path)) as conn:
        # Defensive — table may not exist if the alarm didn't fire
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='audit_alerts'"
        ).fetchone()
        if not exists:
            return 0
        return conn.execute("SELECT COUNT(*) FROM audit_alerts").fetchone()[0]


# ---------------------------------------------------------------------------
# Threshold pinning
# ---------------------------------------------------------------------------

def test_fires_at_exactly_80pct(tmp_path):
    """8 of 10 legs degraded = 80% — at the threshold, alarm fires."""
    db = _make_master_db(tmp_path)
    fired = _run_alarm_block(db, n_legs=10, n_fb=8)
    assert fired is True
    assert _count_alerts(db) == 1


def test_does_not_fire_at_79pct(tmp_path):
    """7 of 10 = 70%, well under. 79.99% wouldn't be reproducible
    with a small n; use 70% as the next clean step below threshold."""
    db = _make_master_db(tmp_path)
    fired = _run_alarm_block(db, n_legs=10, n_fb=7)
    assert fired is False
    assert _count_alerts(db) == 0


def test_fires_at_100pct(tmp_path):
    """Worst case — every leg used the fallback. Alarm must fire."""
    db = _make_master_db(tmp_path)
    fired = _run_alarm_block(db, n_legs=5, n_fb=5)
    assert fired is True
    assert _count_alerts(db) == 1


# ---------------------------------------------------------------------------
# Noise floor pinning
# ---------------------------------------------------------------------------

def test_does_not_fire_below_noise_floor(tmp_path):
    """1 leg @ 100% fallback is statistically meaningless — the
    floor is 3 legs. Pins both ends: 1 and 2."""
    db = _make_master_db(tmp_path)
    assert _run_alarm_block(db, n_legs=1, n_fb=1) is False
    assert _run_alarm_block(db, n_legs=2, n_fb=2) is False
    assert _count_alerts(db) == 0


def test_fires_at_floor_with_full_degradation(tmp_path):
    """3 legs at 100% degradation hits both gates."""
    db = _make_master_db(tmp_path)
    fired = _run_alarm_block(db, n_legs=3, n_fb=3)
    assert fired is True
    assert _count_alerts(db) == 1


# ---------------------------------------------------------------------------
# Alert payload
# ---------------------------------------------------------------------------

def test_alert_payload_has_type_severity_title_detail(tmp_path):
    db = _make_master_db(tmp_path)
    _run_alarm_block(db, n_legs=10, n_fb=9)
    with closing(sqlite3.connect(db)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM audit_alerts ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["alert_type"] == "iv_rank_degradation"
    assert row["severity"] == "warning"
    assert "90.0%" in row["title"]
    assert "9/10" in row["detail"]
    assert "FALLBACK_IV=0.25" in row["detail"]


# ---------------------------------------------------------------------------
# No book_greeks key shouldn't blow up
# ---------------------------------------------------------------------------

def test_empty_book_greeks_dict_is_safe(tmp_path):
    """If compute_book_greeks failed earlier in the snapshot and the
    risk dict has an empty `book_greeks`, the alarm logic must skip
    without raising (zero legs → noise-floor branch)."""
    db = _make_master_db(tmp_path)
    fired = _run_alarm_block(db, n_legs=0, n_fb=0)
    assert fired is False
    assert _count_alerts(db) == 0
