"""Pin Phase B1 data-collection upgrade (2026-05-19).

Three coupled changes the test suite must keep alive:

  1. `record_prediction` accepts + persists the fine-tune-quality
     fields (cycle_id, prompt_text, raw_response, meta_model_score,
     online_meta_score).
  2. New `ai_cycles` table captures append-only cross-candidate
     context so cycle history survives past the next `_save_cycle_data`
     overwrite of `cycle_data_{profile_id}.json`.
  3. `predictions_archive.archive_predictions` writes JSONL files
     for ai_predictions + ai_cycles + specialist_outcomes BEFORE
     the reset wipe, so the fine-tune corpus accumulates across
     experiment generations instead of resetting to zero every
     few weeks.

If any of these regresses, the entire fine-tune dataset story is
back to square one.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


# ---------------------------------------------------------------------------
# Fixture — per-profile DB with full schema
# ---------------------------------------------------------------------------

@pytest.fixture
def profile_db(tmp_path):
    """Build a per-profile DB through journal.init_db so the schema +
    migration both run. Returns the path."""
    from journal import init_db
    db_path = str(tmp_path / "profile.db")
    init_db(db_path)
    return db_path


# ---------------------------------------------------------------------------
# (1) record_prediction round-trip with new fields
# ---------------------------------------------------------------------------

def test_record_prediction_persists_cycle_id_and_prompt(profile_db):
    from ai_tracker import record_prediction
    pred_id = record_prediction(
        symbol="AAPL", predicted_signal="BUY", confidence=72,
        reasoning="bullish",
        price_at_prediction=180.0,
        db_path=profile_db,
        cycle_id="abc123",
        prompt_text="full AI prompt text here",
        raw_response={"trades": [{"symbol": "AAPL", "action": "BUY"}],
                       "portfolio_reasoning": "looks good"},
        meta_model_score=0.78,
        online_meta_score=0.71,
    )
    assert pred_id > 0
    with closing(sqlite3.connect(profile_db)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT cycle_id, prompt_text, raw_response_json, "
            "meta_model_score, online_meta_score "
            "FROM ai_predictions WHERE id=?", (pred_id,),
        ).fetchone()
    assert row["cycle_id"] == "abc123"
    assert row["prompt_text"] == "full AI prompt text here"
    parsed = json.loads(row["raw_response_json"])
    assert parsed["trades"][0]["symbol"] == "AAPL"
    assert row["meta_model_score"] == pytest.approx(0.78)
    assert row["online_meta_score"] == pytest.approx(0.71)


def test_record_prediction_legacy_call_still_works(profile_db):
    """Backward compat: callers that don't pass the new fields still
    succeed; new columns default to NULL."""
    from ai_tracker import record_prediction
    pred_id = record_prediction(
        symbol="MSFT", predicted_signal="HOLD", confidence=0,
        reasoning="", price_at_prediction=400.0, db_path=profile_db,
    )
    assert pred_id > 0
    with closing(sqlite3.connect(profile_db)) as conn:
        row = conn.execute(
            "SELECT cycle_id, prompt_text, raw_response_json "
            "FROM ai_predictions WHERE id=?", (pred_id,),
        ).fetchone()
    assert row == (None, None, None)


# ---------------------------------------------------------------------------
# (2) ai_cycles table exists + accepts a full cycle row
# ---------------------------------------------------------------------------

def test_ai_cycles_table_accepts_full_cycle_row(profile_db):
    with closing(sqlite3.connect(profile_db)) as conn:
        conn.execute(
            """INSERT INTO ai_cycles
               (cycle_id, profile_id, regime, vix, ai_reasoning,
                shortlist_json, market_context_json, sector_rotation_json,
                learned_patterns_json, meta_model_stats_json,
                ensemble_summary_json, n_trades_selected,
                n_candidates_in_shortlist)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "cyc-1", 12, "bull", 18.5, "reasoning text",
                json.dumps([{"symbol": "AAPL", "signal": "BUY"}]),
                json.dumps({"yield_curve": "normal"}),
                json.dumps({"sector": "tech_leadership"}),
                json.dumps([]),
                json.dumps({"loaded": True, "suppressed": 3}),
                json.dumps({"enabled": True}),
                1, 5,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM ai_cycles WHERE cycle_id=?", ("cyc-1",),
        ).fetchone()
    assert row is not None


def test_cycle_id_links_predictions_to_their_cycle(profile_db):
    """The whole point of the cycle_id FK: at training time you can
    join ai_predictions.cycle_id → ai_cycles.cycle_id to reconstruct
    the full cross-candidate context for any prediction."""
    from ai_tracker import record_prediction
    with closing(sqlite3.connect(profile_db)) as conn:
        conn.execute(
            "INSERT INTO ai_cycles (cycle_id, profile_id) VALUES (?, ?)",
            ("cyc-2", 12),
        )
        conn.commit()
    p1 = record_prediction(
        symbol="AAPL", predicted_signal="BUY", confidence=70,
        reasoning="", price_at_prediction=180.0, db_path=profile_db,
        cycle_id="cyc-2",
    )
    p2 = record_prediction(
        symbol="MSFT", predicted_signal="HOLD", confidence=0,
        reasoning="", price_at_prediction=400.0, db_path=profile_db,
        cycle_id="cyc-2",
    )
    with closing(sqlite3.connect(profile_db)) as conn:
        rows = conn.execute(
            "SELECT p.symbol FROM ai_predictions p "
            "JOIN ai_cycles c ON p.cycle_id = c.cycle_id "
            "WHERE c.cycle_id = ?", ("cyc-2",),
        ).fetchall()
    assert sorted(r[0] for r in rows) == ["AAPL", "MSFT"]


# ---------------------------------------------------------------------------
# (3) predictions_archive.archive_predictions round-trip
# ---------------------------------------------------------------------------

def _seed_archive_data(db_path, n_predictions=3, n_cycles=2,
                       n_specialist_outcomes=4):
    from ai_tracker import record_prediction
    with closing(sqlite3.connect(db_path)) as conn:
        for i in range(n_cycles):
            conn.execute(
                "INSERT INTO ai_cycles (cycle_id, profile_id, regime) "
                "VALUES (?, ?, ?)",
                (f"cyc-{i}", 12, "bull"),
            )
        # specialist_outcomes — create the table if init_db didn't
        conn.execute("""
            CREATE TABLE IF NOT EXISTS specialist_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prediction_id INTEGER NOT NULL,
                specialist_name TEXT NOT NULL,
                verdict TEXT NOT NULL,
                raw_confidence INTEGER NOT NULL,
                recorded_at TEXT NOT NULL DEFAULT (datetime('now')),
                was_correct INTEGER,
                resolved_at TEXT,
                UNIQUE(prediction_id, specialist_name))
        """)
        for i in range(n_specialist_outcomes):
            conn.execute(
                "INSERT INTO specialist_outcomes "
                "(prediction_id, specialist_name, verdict, raw_confidence) "
                "VALUES (?, ?, ?, ?)",
                (i + 1, f"specialist_{i}", "BUY", 75),
            )
        conn.commit()
    for i in range(n_predictions):
        record_prediction(
            symbol=f"SYM{i}", predicted_signal="BUY", confidence=70,
            reasoning="t", price_at_prediction=100.0 + i,
            db_path=db_path, cycle_id=f"cyc-{i % n_cycles}",
        )


def test_archive_writes_jsonl_files_per_table(profile_db, tmp_path):
    from predictions_archive import archive_predictions
    _seed_archive_data(profile_db, n_predictions=3, n_cycles=2,
                       n_specialist_outcomes=4)
    out_root = tmp_path / "archive"
    counts = archive_predictions(
        db_path=profile_db, profile_id=12,
        archive_root=str(out_root),
        reset_timestamp="20260519_220000",
    )
    assert counts == {"predictions": 3, "cycles": 2, "specialist_outcomes": 4}

    # Files exist on disk
    out_dir = out_root / "12" / "20260519_220000"
    assert (out_dir / "predictions.jsonl").exists()
    assert (out_dir / "cycles.jsonl").exists()
    assert (out_dir / "specialist_outcomes.jsonl").exists()

    # Round-trip: each line parses as JSON and contains expected keys
    pred_lines = (out_dir / "predictions.jsonl").read_text().splitlines()
    assert len(pred_lines) == 3
    for line in pred_lines:
        d = json.loads(line)
        assert "symbol" in d
        assert "predicted_signal" in d
        assert "cycle_id" in d
        assert "prompt_text" in d  # new column present in archive


def test_archive_handles_missing_db_path_gracefully(tmp_path):
    """A profile DB that doesn't exist returns {} — doesn't raise.
    The reset script can iterate active profiles without per-profile
    error handling."""
    from predictions_archive import archive_predictions
    counts = archive_predictions(
        db_path=str(tmp_path / "nonexistent.db"),
        profile_id=999,
        archive_root=str(tmp_path / "archive"),
    )
    assert counts == {}


def test_archive_handles_missing_ai_cycles_table_gracefully(tmp_path):
    """If a DB happens to be missing the ai_cycles table (corruption,
    partial schema), archive must still dump the tables that DO exist
    rather than aborting. Seed via raw INSERT into a minimal schema
    so we're testing the archive's tolerance, not record_prediction's
    schema needs."""
    from predictions_archive import archive_predictions
    db_path = str(tmp_path / "minimal.db")
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("""
            CREATE TABLE ai_predictions (
                id INTEGER PRIMARY KEY, symbol TEXT,
                predicted_signal TEXT)
        """)
        conn.execute(
            "INSERT INTO ai_predictions (symbol, predicted_signal) "
            "VALUES ('X', 'BUY')",
        )
        conn.commit()
    counts = archive_predictions(
        db_path=db_path, profile_id=99,
        archive_root=str(tmp_path / "archive"),
        reset_timestamp="t",
    )
    assert counts["predictions"] == 1
    assert counts["cycles"] == 0  # table missing, empty file written
    assert counts["specialist_outcomes"] == 0  # table missing too


# ---------------------------------------------------------------------------
# (4) Reset script integration — archive happens BEFORE wipe
# ---------------------------------------------------------------------------

def test_reset_script_calls_archive_before_wipe():
    """Source-level pin: reset_for_clean_experiment.py must call
    archive_predictions inside the APPLY path BEFORE the truncate
    loop runs. The dry-run path counts what would be wiped but
    doesn't actually wipe — only the apply path needs the archive."""
    src = (REPO / "reset_for_clean_experiment.py").read_text()
    # Anchor to the "Real wipe:" comment so we get the apply-path
    # subsegment, not the dry-run loop above it that also mentions
    # _ALWAYS_WIPE.
    apply_path_start = src.find("# Real wipe:")
    assert apply_path_start > 0, (
        "reset_for_clean_experiment.py is missing its 'Real wipe:' "
        "marker — the test can't anchor the apply-path block"
    )
    apply_path = src[apply_path_start:]
    archive_idx = apply_path.find("from predictions_archive import")
    truncate_idx = apply_path.find("for t in _ALWAYS_WIPE")
    assert archive_idx > 0, (
        "Apply path must import + call archive_predictions"
    )
    assert truncate_idx > 0, "Apply path must run the wipe loop"
    assert archive_idx < truncate_idx, (
        "Within the apply path, archive_predictions must come BEFORE "
        "the wipe loop. If wipe runs first, the archive captures "
        "nothing — the entire fine-tune corpus is lost."
    )
    # The failure path must RAISE (not log-and-continue) so the
    # wipe is aborted on archive failure.
    assert "REFUSING TO WIPE" in src.upper() or "raise\n" in apply_path, (
        "archive failure must abort the wipe — raise after logging "
        "the error, never silently continue to the truncate."
    )
