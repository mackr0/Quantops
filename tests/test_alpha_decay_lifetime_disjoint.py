"""Guardrail: alpha_decay's "lifetime" baseline must EXCLUDE the
recent rolling window so the rolling-vs-lifetime comparison reads
disjoint periods.

History: 2026-04-27 methodology audit. Wave 3 / Fix #8 of
METHODOLOGY_FIX_PLAN.md. The original `compute_lifetime_metrics`
queried ALL resolved predictions, which meant the rolling-window
data was INCLUDED in the lifetime baseline. When `detect_decay`
compared rolling vs lifetime Sharpe to detect degradation, the
comparison was less sensitive than it should be — both sides
shared the most-recent data, dampening decay signals.

Fix: `compute_lifetime_metrics` accepts `exclude_recent_days`
(default 30, matching the rolling window). `detect_decay` and
`should_restore` pass `rolling_window_days` to enforce strict
separation.

Plus a contract test for Fix #7 (strategy_lifecycle): verify
`_run_validation` calls `validate_strategy` so it inherits the
date-range fixes from Wave 2.
"""

from __future__ import annotations

import inspect
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta

import pytest

import alpha_decay
import strategy_lifecycle


# ---------------------------------------------------------------------------
# Source-level guardrails for Fix #8
# ---------------------------------------------------------------------------

def test_compute_lifetime_metrics_accepts_exclude_recent_days():
    sig = inspect.signature(alpha_decay.compute_lifetime_metrics)
    assert "exclude_recent_days" in sig.parameters, (
        "REGRESSION: compute_lifetime_metrics must accept "
        "exclude_recent_days. Without it, the lifetime baseline "
        "includes the rolling window — biasing decay detection."
    )


def test_detect_decay_excludes_rolling_window_from_lifetime():
    """detect_decay must pass exclude_recent_days when calling
    compute_lifetime_metrics. Otherwise the Wave 3 fix is inert."""
    src = inspect.getsource(alpha_decay.detect_decay)
    assert "exclude_recent_days" in src, (
        "REGRESSION: detect_decay no longer excludes the rolling "
        "window from the lifetime baseline. The disjoint-window "
        "comparison is the whole point of Wave 3 / Fix #8."
    )


def test_check_restoration_excludes_rolling_window_from_lifetime():
    src = inspect.getsource(alpha_decay.check_restoration)
    assert "exclude_recent_days" in src, (
        "check_restoration must also exclude rolling window data "
        "from lifetime metrics — same disjointness rule applies to "
        "restoration logic."
    )


# ---------------------------------------------------------------------------
# Behavioral guardrails for Fix #8
# ---------------------------------------------------------------------------

@pytest.fixture
def seeded_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE ai_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_type TEXT NOT NULL,
            status TEXT,
            actual_outcome TEXT,
            actual_return_pct REAL,
            resolved_at TEXT
        )
    """)
    conn.commit()
    conn.close()
    yield path
    os.unlink(path)


def _insert(db, strategy_type, outcome, return_pct, resolved_days_ago):
    conn = sqlite3.connect(db)
    resolved_at = (datetime.utcnow() - timedelta(days=resolved_days_ago)).isoformat()
    conn.execute(
        "INSERT INTO ai_predictions "
        "(strategy_type, status, actual_outcome, actual_return_pct, resolved_at) "
        "VALUES (?, 'resolved', ?, ?, ?)",
        (strategy_type, outcome, return_pct, resolved_at),
    )
    conn.commit()
    conn.close()


def test_lifetime_excludes_rolling_window_data(seeded_db):
    """Old data is profitable; recent rolling-window data is
    losing. Lifetime metrics with exclude_recent_days=30 must reflect
    only the old (profitable) period."""
    # Older data (45-90 days ago): all wins
    for d in range(45, 90):
        _insert(seeded_db, "test_strategy", "win", 2.0, d)
    # Recent rolling window (5-25 days ago): all losses
    for d in range(5, 25):
        _insert(seeded_db, "test_strategy", "loss", -2.0, d)

    # With exclude_recent_days=0 (legacy behavior): mixed
    legacy = alpha_decay.compute_lifetime_metrics(
        seeded_db, "test_strategy", exclude_recent_days=0,
    )
    # With exclude_recent_days=30 (the fix): old data only
    fixed = alpha_decay.compute_lifetime_metrics(
        seeded_db, "test_strategy", exclude_recent_days=30,
    )

    # Legacy includes everything; fixed only includes the old wins.
    assert fixed["n_predictions"] < legacy["n_predictions"], (
        f"Fixed lifetime should have fewer predictions than legacy "
        f"(it excluded the rolling window). "
        f"legacy={legacy['n_predictions']}, fixed={fixed['n_predictions']}"
    )
    assert fixed["win_rate"] > legacy["win_rate"], (
        f"Fixed lifetime should reflect only the old (profitable) "
        f"data, so win rate should be higher than the contaminated "
        f"legacy figure. legacy_wr={legacy['win_rate']:.0f}%, "
        f"fixed_wr={fixed['win_rate']:.0f}%"
    )


def test_lifetime_with_exclude_recent_days_zero_matches_legacy_behavior(seeded_db):
    """Backwards-compat: exclude_recent_days=0 returns the old "all
    resolved predictions" result so legacy callers (if any) keep
    working."""
    for d in [10, 30, 60, 90]:
        _insert(seeded_db, "compat", "win", 1.0, d)

    legacy = alpha_decay.compute_lifetime_metrics(
        seeded_db, "compat", exclude_recent_days=0,
    )
    assert legacy["n_predictions"] == 4, (
        f"With exclude=0 we should see all 4 resolved predictions, "
        f"got {legacy['n_predictions']}"
    )


# ---------------------------------------------------------------------------
# Fix #7 contract test — strategy_lifecycle uses the fixed gates
# ---------------------------------------------------------------------------

def test_strategy_lifecycle_validation_uses_validate_strategy():
    """`_run_validation` must call validate_strategy from
    rigorous_backtest. validate_strategy in turn calls our (now
    fixed) walk_forward_analysis and out_of_sample_degradation, so
    auto-strategies inherit the disjoint-window discipline."""
    src = inspect.getsource(strategy_lifecycle._run_validation)
    assert "validate_strategy" in src, (
        "REGRESSION: strategy_lifecycle._run_validation no longer "
        "calls validate_strategy. Auto-strategies would be promoted "
        "without the disjoint-window gates from Wave 2 / Fixes #3 #4."
    )
