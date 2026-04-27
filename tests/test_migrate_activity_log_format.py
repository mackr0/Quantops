"""Tests for migrate_activity_log_format.py — confirms the regex
rewrites produce the expected friendly format and are idempotent.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

import migrate_activity_log_format as mig


def test_rewrites_user_reported_past_adjustment_format():
    """The exact string the user saw: snake_case + raw decimals."""
    bad = ("PAST ADJUSTMENT REVIEWS:\n  - Reviewed past adjustment: "
           "max_position_pct 0.08->0.092 (win rate 48%->52%: IMPROVED)")
    new = mig.rewrite_detail(bad)
    # Param name → display label
    assert "max_position_pct" not in new, (
        f"Raw param name still present in rewrite: {new!r}"
    )
    assert "Max Position Size" in new
    # Decimals → percentage formatting
    assert "0.08" not in new
    assert "0.092" not in new
    assert "8.0%" in new
    assert "9.2%" in new
    # Win-rate cosmetic arrow
    assert "48%->52%" not in new
    assert "48% → 52%" in new


def test_rewrites_reversed_message_format():
    bad = ("REVERSED: stop_loss_pct back from 0.05 to 0.03 "
           "(previous change worsened performance)")
    new = mig.rewrite_detail(bad)
    assert "stop_loss_pct" not in new
    # Display name registry maps stop_loss_pct → "Stop-Loss (%)" — the
    # exact text varies, but it must NOT be the raw snake_case key.
    assert "Stop" in new and "Loss" in new
    assert "5.0%" in new
    assert "3.0%" in new


def test_rewrites_adjusting_summary_lines():
    bad = "- Adjusting max_correlation: has worked well"
    new = mig.rewrite_detail(bad)
    assert "max_correlation" not in new
    assert "Max Correlation" in new


def test_idempotent_on_already_rewritten_text():
    """Run the rewrite twice — second pass must be a no-op (so
    re-running the migration is safe)."""
    bad = ("Reviewed past adjustment: max_position_pct 0.08->0.092 "
           "(win rate 48%->52%: IMPROVED)")
    once = mig.rewrite_detail(bad)
    twice = mig.rewrite_detail(once)
    assert once == twice, (
        f"Second pass changed the output. First={once!r}, second={twice!r}"
    )


def test_unrelated_text_passes_through_unchanged():
    """Don't accidentally rewrite English text that happens to look
    like snake_case (e.g., 'has_options', 'easy_to_borrow' — random
    Alpaca attribute names that shouldn't be touched)."""
    benign = ("AAPL has_options: true (from Alpaca asset attributes); "
              "easy_to_borrow: true. Nothing to do here.")
    assert mig.rewrite_detail(benign) == benign


def test_unknown_snake_case_param_is_not_rewritten():
    """Regex is permissive — actual rewrite only applies if the
    matched name is in PARAM_BOUNDS. Made-up names pass through."""
    bad = "Reviewed past adjustment: zzz_made_up_param 1.0->2.0"
    assert mig.rewrite_detail(bad) == bad


def test_migrate_function_end_to_end_with_real_db():
    """End-to-end: seed a temp DB, run migrate(), assert rows are
    rewritten in place."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                timestamp TEXT,
                activity_type TEXT,
                title TEXT,
                detail TEXT
            )
        """)
        conn.execute(
            "INSERT INTO activity_log "
            "(profile_id, user_id, activity_type, title, detail) "
            "VALUES (1, 1, 'self_tune', 'Test', "
            "'PAST ADJUSTMENT REVIEWS:\n  - Reviewed past adjustment: "
            "max_position_pct 0.08->0.092 (win rate 48%->52%: IMPROVED)')"
        )
        # Add an unrelated row to ensure we don't touch it
        conn.execute(
            "INSERT INTO activity_log "
            "(profile_id, user_id, activity_type, title, detail) "
            "VALUES (1, 1, 'trade', 'BUY AAPL', 'Bought 10 shares at $200')"
        )
        conn.commit()
        conn.close()

        stats = mig.migrate(path)
        assert stats["rewritten"] == 1
        assert stats["scanned"] >= 1

        conn = sqlite3.connect(path)
        details = [r[0] for r in conn.execute("SELECT detail FROM activity_log").fetchall()]
        conn.close()
        # The bad row was rewritten
        rewritten = [d for d in details if "Max Position Size" in d]
        assert len(rewritten) == 1, f"Expected 1 rewritten row, got: {details}"
        # The trade row was untouched
        assert any("Bought 10 shares" in d for d in details)
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass


def test_dry_run_does_not_write():
    """`--dry-run` reports counts without committing changes."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER, user_id INTEGER,
                timestamp TEXT, activity_type TEXT, title TEXT, detail TEXT
            )
        """)
        bad = ("Reviewed past adjustment: stop_loss_pct 0.05->0.03 "
               "(win rate 60%->58%: WORSENED)")
        conn.execute(
            "INSERT INTO activity_log (profile_id, user_id, detail) "
            "VALUES (1, 1, ?)",
            (bad,),
        )
        conn.commit()
        conn.close()

        stats = mig.migrate(path, dry_run=True)
        assert stats["rewritten"] == 1

        # Verify the row is UNCHANGED (dry-run doesn't write)
        conn = sqlite3.connect(path)
        row = conn.execute("SELECT detail FROM activity_log").fetchone()
        conn.close()
        assert row[0] == bad, (
            "Dry-run should not write. Original was preserved? "
            f"got: {row[0]!r}"
        )
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass
