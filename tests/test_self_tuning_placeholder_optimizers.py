"""Behavioral tests for the two formerly-placeholder optimizers in
self_tuning.py: `_optimize_skip_first_minutes` and
`_optimize_avoid_earnings_days`.

Both rules used to `return None` unconditionally (placeholders
registered for orchestration but no-ops). On 2026-04-27 they were
implemented for real:
- skip_first_minutes — buckets resolved predictions by
  minutes-since-market-open (parsed from timestamp); tighter/looser
  recommendations when early-window WR materially differs from late.
- avoid_earnings_days — buckets by `days_to_earnings` (now captured
  in features_json); same recommendation pattern.

These tests prove the rules:
1. Self-skip when not enough data in either bucket.
2. Recommend tightening when in-window underperforms.
3. Recommend loosening when in-window outperforms.
4. Don't fire on noise (small bucket count).
"""

from __future__ import annotations

import json as _json
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _mock_models():
    """Patch update_trading_profile + log_tuning_change so the
    optimizers can run end-to-end without a real master DB."""
    return patch.multiple(
        "models",
        update_trading_profile=lambda *a, **kw: None,
        log_tuning_change=lambda *a, **kw: None,
    )


@pytest.fixture
def seeded_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE ai_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            status TEXT,
            actual_outcome TEXT,
            features_json TEXT,
            confidence INTEGER
        )
    """)
    conn.commit()
    conn.close()
    yield path
    os.unlink(path)


def _insert(db, *, timestamp_iso, outcome, days_to_earnings=None):
    feats = {}
    if days_to_earnings is not None:
        feats["days_to_earnings"] = days_to_earnings
    fjson = _json.dumps(feats) if feats else None
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO ai_predictions "
        "(timestamp, status, actual_outcome, features_json) "
        "VALUES (?, 'resolved', ?, ?)",
        (timestamp_iso, outcome, fjson),
    )
    conn.commit()
    conn.close()


def _ctx(skip_first_minutes=0, avoid_earnings_days=2):
    return SimpleNamespace(
        skip_first_minutes=skip_first_minutes,
        avoid_earnings_days=avoid_earnings_days,
    )


# ---------------------------------------------------------------------------
# skip_first_minutes
# ---------------------------------------------------------------------------

def _ts_at_minute(minutes_after_open: int) -> str:
    """Build an ISO timestamp at 13:30 UTC + minutes_after_open."""
    base = datetime(2026, 4, 27, 13, 30, 0)
    return (base + timedelta(minutes=minutes_after_open)).isoformat()


def test_skip_first_minutes_self_skips_below_threshold(seeded_db):
    from self_tuning import _optimize_skip_first_minutes
    # Only 5 early predictions — below the 10-min-per-bucket gate
    for _ in range(5):
        _insert(seeded_db, timestamp_iso=_ts_at_minute(10), outcome="win")
    for _ in range(20):
        _insert(seeded_db, timestamp_iso=_ts_at_minute(120), outcome="win")
    conn = sqlite3.connect(seeded_db)
    try:
        result = _optimize_skip_first_minutes(conn, _ctx(), 1, 1, 50, 25)
    finally:
        conn.close()
    assert result is None


def test_skip_first_minutes_recommends_tighten_when_early_underperforms(seeded_db):
    """Early predictions: 30% win rate. Late predictions: 65% win
    rate. Difference > 5pp triggers a tighten recommendation."""
    from self_tuning import _optimize_skip_first_minutes
    # 20 early, 30% wins
    for i in range(20):
        outcome = "win" if i < 6 else "loss"
        _insert(seeded_db, timestamp_iso=_ts_at_minute(10), outcome=outcome)
    # 20 late, 65% wins
    for i in range(20):
        outcome = "win" if i < 13 else "loss"
        _insert(seeded_db, timestamp_iso=_ts_at_minute(120), outcome=outcome)

    conn = sqlite3.connect(seeded_db)
    try:
        with _mock_models():
            result = _optimize_skip_first_minutes(
                conn, _ctx(skip_first_minutes=0), 1, 1, 50, 40,
            )
    finally:
        conn.close()
    assert result is not None, (
        "Early-window predictions winning 30% vs late winning 65% "
        "should trigger a tighten recommendation."
    )
    assert isinstance(result, str)
    assert "raised" in result.lower() or "tighten" in result.lower(), (
        f"Expected description to mention raising/tightening, got: {result!r}"
    )


def test_skip_first_minutes_recommends_loosen_when_early_fine(seeded_db):
    """If early predictions are actually fine and we currently skip
    20 minutes, loosen toward 0."""
    from self_tuning import _optimize_skip_first_minutes
    # 20 early, 70% wins
    for i in range(20):
        outcome = "win" if i < 14 else "loss"
        _insert(seeded_db, timestamp_iso=_ts_at_minute(10), outcome=outcome)
    # 20 late, 60% wins (similar to early)
    for i in range(20):
        outcome = "win" if i < 12 else "loss"
        _insert(seeded_db, timestamp_iso=_ts_at_minute(120), outcome=outcome)

    conn = sqlite3.connect(seeded_db)
    try:
        with _mock_models():
            result = _optimize_skip_first_minutes(
                conn, _ctx(skip_first_minutes=20), 1, 1, 65, 40,
            )
    finally:
        conn.close()
    assert result is not None
    assert isinstance(result, str)
    assert "lowered" in result.lower() or "loosen" in result.lower(), (
        f"Expected description to mention lowering/loosening, got: {result!r}"
    )


# ---------------------------------------------------------------------------
# avoid_earnings_days
# ---------------------------------------------------------------------------

def test_avoid_earnings_self_skips_below_threshold(seeded_db):
    from self_tuning import _optimize_avoid_earnings_days
    # Only 3 in-window predictions
    for _ in range(3):
        _insert(seeded_db, timestamp_iso=_ts_at_minute(60),
                 outcome="win", days_to_earnings=1)
    for _ in range(30):
        _insert(seeded_db, timestamp_iso=_ts_at_minute(60),
                 outcome="win", days_to_earnings=10)
    conn = sqlite3.connect(seeded_db)
    try:
        result = _optimize_avoid_earnings_days(conn, _ctx(), 1, 1, 70, 33)
    finally:
        conn.close()
    assert result is None


def test_avoid_earnings_tightens_when_in_window_underperforms(seeded_db):
    """In-window (≤2 days to earnings): 30% wins. Out-of-window:
    65% wins. Tighten."""
    from self_tuning import _optimize_avoid_earnings_days
    # 20 in-window, 30% wins
    for i in range(20):
        outcome = "win" if i < 6 else "loss"
        _insert(seeded_db, timestamp_iso=_ts_at_minute(60),
                 outcome=outcome, days_to_earnings=1)
    # 20 out-of-window, 65% wins
    for i in range(20):
        outcome = "win" if i < 13 else "loss"
        _insert(seeded_db, timestamp_iso=_ts_at_minute(60),
                 outcome=outcome, days_to_earnings=10)

    conn = sqlite3.connect(seeded_db)
    try:
        with _mock_models():
            result = _optimize_avoid_earnings_days(
                conn, _ctx(avoid_earnings_days=2), 1, 1, 50, 40,
            )
    finally:
        conn.close()
    assert result is not None
    assert isinstance(result, str)
    assert "tighten" in result.lower(), (
        f"Expected description to mention tightening, got: {result!r}"
    )


def test_avoid_earnings_loosens_when_in_window_outperforms(seeded_db):
    """In-window predictions are actually doing BETTER than
    out-of-window (e.g., catching post-earnings drift right after the
    print). Lower avoidance days."""
    from self_tuning import _optimize_avoid_earnings_days
    # 20 in-window, 75% wins
    for i in range(20):
        outcome = "win" if i < 15 else "loss"
        _insert(seeded_db, timestamp_iso=_ts_at_minute(60),
                 outcome=outcome, days_to_earnings=1)
    # 20 out-of-window, 55% wins
    for i in range(20):
        outcome = "win" if i < 11 else "loss"
        _insert(seeded_db, timestamp_iso=_ts_at_minute(60),
                 outcome=outcome, days_to_earnings=10)

    conn = sqlite3.connect(seeded_db)
    try:
        with _mock_models():
            result = _optimize_avoid_earnings_days(
                conn, _ctx(avoid_earnings_days=2), 1, 1, 65, 40,
            )
    finally:
        conn.close()
    assert result is not None
    assert isinstance(result, str)
    assert "loosen" in result.lower(), (
        f"Expected description to mention loosening, got: {result!r}"
    )


def test_trailing_atr_multiplier_self_skips_below_min_samples(seeded_db):
    """Need ≥30 closed longs with non-null MFE before reading
    statistical signal."""
    from self_tuning import _optimize_trailing_atr_multiplier
    # Add only 5 closed-sell rows — well below the 30 threshold
    conn = sqlite3.connect(seeded_db)
    conn.execute(
        "ALTER TABLE ai_predictions ADD COLUMN dummy TEXT"
    )  # noop just to keep schema fixture compatible
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, side TEXT, status TEXT,
            qty REAL, price REAL, fill_price REAL,
            pnl REAL, max_favorable_excursion REAL
        )
    """)
    for _ in range(5):
        conn.execute(
            "INSERT INTO trades (symbol, side, status, qty, price, "
            "fill_price, pnl, max_favorable_excursion) "
            "VALUES ('AAPL', 'sell', 'closed', 100, 95, 95, 5, 100)"
        )
    conn.commit()
    ctx = SimpleNamespace(
        use_trailing_stops=True, trailing_atr_multiplier=1.5,
    )
    try:
        with patch("self_tuning._safe_change_guarded", return_value=True):
            with _mock_models():
                result = _optimize_trailing_atr_multiplier(
                    conn, ctx, 1, 1, 50, 5,
                )
    finally:
        conn.close()
    assert result is None


def test_trailing_atr_multiplier_tightens_on_excessive_give_back(seeded_db):
    """Average give-back > 50% triggers tightening (winners evaporate
    too much before exit)."""
    from self_tuning import _optimize_trailing_atr_multiplier
    conn = sqlite3.connect(seeded_db)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, side TEXT, status TEXT,
            qty REAL, price REAL, fill_price REAL,
            pnl REAL, max_favorable_excursion REAL
        )
    """)
    # 50 trades: each ran to 200 (MFE), exited at 95 → give-back 52.5%
    for _ in range(50):
        conn.execute(
            "INSERT INTO trades (symbol, side, status, qty, price, "
            "fill_price, pnl, max_favorable_excursion) "
            "VALUES ('XYZ', 'sell', 'closed', 100, 95, 95, -2, 200)"
        )
    conn.commit()
    ctx = SimpleNamespace(
        use_trailing_stops=True, trailing_atr_multiplier=1.5,
    )
    try:
        with patch("self_tuning._safe_change_guarded", return_value=True):
            with _mock_models():
                result = _optimize_trailing_atr_multiplier(
                    conn, ctx, 1, 1, 50, 50,
                )
    finally:
        conn.close()
    assert result is not None
    assert "tighten" in result.lower(), (
        f"Excessive give-back must trigger tightening, got: {result!r}"
    )


def test_trailing_atr_multiplier_loosens_on_small_give_back_with_profit(seeded_db):
    """Small give-back + positive pnl means winners exit near peak
    — possibly being whipsawed out; loosen."""
    from self_tuning import _optimize_trailing_atr_multiplier
    conn = sqlite3.connect(seeded_db)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, side TEXT, status TEXT,
            qty REAL, price REAL, fill_price REAL,
            pnl REAL, max_favorable_excursion REAL
        )
    """)
    # 50 trades: MFE 100, exit 95 → give-back 5%, pnl positive
    for _ in range(50):
        conn.execute(
            "INSERT INTO trades (symbol, side, status, qty, price, "
            "fill_price, pnl, max_favorable_excursion) "
            "VALUES ('XYZ', 'sell', 'closed', 100, 95, 95, 5, 100)"
        )
    conn.commit()
    ctx = SimpleNamespace(
        use_trailing_stops=True, trailing_atr_multiplier=1.5,
    )
    try:
        with patch("self_tuning._safe_change_guarded", return_value=True):
            with _mock_models():
                result = _optimize_trailing_atr_multiplier(
                    conn, ctx, 1, 1, 70, 50,
                )
    finally:
        conn.close()
    assert result is not None
    assert "loosen" in result.lower()


def test_avoid_earnings_skips_predictions_without_feature(seeded_db):
    """Pre-2026-04-27 predictions have features_json without
    days_to_earnings. Those rows must be excluded from the buckets;
    if all buckets are empty after exclusion, return None."""
    from self_tuning import _optimize_avoid_earnings_days
    for _ in range(40):
        # No days_to_earnings field in features_json
        _insert(seeded_db, timestamp_iso=_ts_at_minute(60), outcome="win")
    conn = sqlite3.connect(seeded_db)
    try:
        result = _optimize_avoid_earnings_days(conn, _ctx(), 1, 1, 100, 40)
    finally:
        conn.close()
    assert result is None
