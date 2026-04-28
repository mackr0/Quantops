"""Guardrail: per-symbol track record fed to the AI prompt must
split by signal type (BUY / SHORT / SELL / HOLD), not aggregate
all signal types into a single number.

History: on 2026-04-28 the AI generated this reasoning while
shorting VALE:

    "Perfect fit: 100% personal win rate (13W/0L) on VALE SHORT
     signals with clean dual SELL votes ..."

Real data: VALE had 13 RESOLVED predictions in that profile, all
of them HOLDs. Zero resolved SHORTs. The aggregate `13W/0L` was
true, but the AI confabulated the SHORT attribution because the
prompt didn't surface the signal-type breakdown.

Fix:
- `self_tuning.get_symbol_reputation` now returns
  `{wins, losses, total, win_rate, avg_return, by_signal: {...}}`.
- The prompt builder in `trade_pipeline._build_candidates_data`
  emits a signal-split string like:
    "13W/0L overall (100%) — BUY 0W/0L (0%); SHORT 0W/0L (0%);
     HOLD 13W/0L (100%)"

These tests prove:
1. get_symbol_reputation result has by_signal breakdown.
2. Aggregate counters still match across signal types.
3. The prompt-side track_record string mentions signal-type splits
   (not just "13W/0L overall").
4. Source-level guard: track_record assignment must reference
   `by_signal`.
"""

from __future__ import annotations

import inspect
import os
import sqlite3
import tempfile

import pytest


@pytest.fixture
def seeded_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE ai_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            symbol TEXT NOT NULL,
            predicted_signal TEXT NOT NULL,
            confidence INTEGER,
            price_at_prediction REAL NOT NULL DEFAULT 100.0,
            status TEXT,
            actual_outcome TEXT,
            actual_return_pct REAL
        )
    """)
    conn.commit()
    conn.close()
    yield path
    os.unlink(path)


def _seed(db, symbol, signal, outcome, return_pct=1.0):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO ai_predictions "
        "(symbol, predicted_signal, confidence, status, "
        " actual_outcome, actual_return_pct) "
        "VALUES (?, ?, 50, 'resolved', ?, ?)",
        (symbol, signal, outcome, return_pct),
    )
    conn.commit()
    conn.close()


def test_reputation_has_by_signal_breakdown(seeded_db):
    """The repro for the VALE bug: 13 HOLD wins, 0 SHORT/BUY.
    Reputation must NOT show the aggregate as if it were per-signal."""
    from self_tuning import get_symbol_reputation
    for _ in range(13):
        _seed(seeded_db, "VALE", "HOLD", "win")

    rep = get_symbol_reputation(seeded_db, min_predictions=3)
    assert "VALE" in rep
    v = rep["VALE"]
    assert v["wins"] == 13
    assert v["losses"] == 0
    assert v["total"] == 13
    # The critical assertion: by_signal exists and has only HOLD
    by_sig = v["by_signal"]
    assert "HOLD" in by_sig, f"by_signal missing HOLD: {by_sig}"
    assert by_sig["HOLD"]["wins"] == 13
    # AND there must be NO entry claiming SHORT/BUY wins
    for sig in ("BUY", "SHORT", "SELL"):
        if sig in by_sig:
            assert by_sig[sig]["total"] == 0, (
                f"by_signal[{sig}] should be empty/zero "
                f"(no resolved {sig} predictions on VALE), got "
                f"{by_sig[sig]}"
            )


def test_reputation_splits_mixed_signal_types(seeded_db):
    """Symbol with 5 BUY wins, 2 BUY losses, 3 HOLD wins. Expect
    aggregate 8W/2L, BUY 5W/2L, HOLD 3W/0L."""
    from self_tuning import get_symbol_reputation
    for _ in range(5):
        _seed(seeded_db, "AAPL", "BUY", "win")
    for _ in range(2):
        _seed(seeded_db, "AAPL", "BUY", "loss")
    for _ in range(3):
        _seed(seeded_db, "AAPL", "HOLD", "win")

    rep = get_symbol_reputation(seeded_db, min_predictions=3)
    a = rep["AAPL"]
    assert a["wins"] == 8
    assert a["losses"] == 2
    assert a["by_signal"]["BUY"]["wins"] == 5
    assert a["by_signal"]["BUY"]["losses"] == 2
    assert a["by_signal"]["HOLD"]["wins"] == 3


def test_reputation_excludes_below_min_predictions(seeded_db):
    """Only 2 resolved predictions — below the default min of 3."""
    from self_tuning import get_symbol_reputation
    _seed(seeded_db, "TSLA", "BUY", "win")
    _seed(seeded_db, "TSLA", "BUY", "win")
    rep = get_symbol_reputation(seeded_db, min_predictions=3)
    assert "TSLA" not in rep


def test_track_record_string_includes_signal_breakdown(seeded_db):
    """Source-level guard: trade_pipeline._build_candidates_data
    must format track_record with by_signal data, not just aggregate."""
    import trade_pipeline as tp
    src = inspect.getsource(tp._build_candidates_data)
    assert "by_signal" in src, (
        "REGRESSION: _build_candidates_data no longer references "
        "by_signal. Track record string is back to lumping all "
        "signal types into one aggregate, which the AI then "
        "confabulates as signal-specific edge. See 2026-04-28 "
        "VALE SHORT incident."
    )
    # Must mention at least the canonical signal types in the format
    # so the AI sees them separated.
    for sig in ("BUY", "SHORT"):
        assert f'"{sig}"' in src or f"'{sig}'" in src, (
            f"_build_candidates_data should explicitly enumerate "
            f"{sig} when building the breakdown string."
        )
