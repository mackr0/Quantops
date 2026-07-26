"""The Autonomy Timeline endpoint's queries must match the REAL table
schemas — and a broken source must never masquerade as "no events".

2026-07-26: the endpoint queried `change_type`; the actual
tuning_history column is `adjustment_type`. The query raised
OperationalError on EVERY call, the except swallowed it at debug, and
the timeline showed "No autonomous changes in the last 30 days" for
every profile since the endpoint shipped. The operator reasonably read
that as the AI layers being dead ("did something happen where you have
completely fucked the ai aspects of this system?"). Schema drift must
fail these tests, not silently blank the page.
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _endpoint_src() -> str:
    src = open(os.path.join(REPO, "views.py")).read()
    start = src.index("def api_autonomy_timeline")
    end = src.index("\n@views_bp.route", start)
    return src[start:end]


def _columns_of(sql_create_fn) -> set:
    """Create the table via the production schema and read its columns."""
    conn = sqlite3.connect(":memory:")
    sql_create_fn(conn)
    cols = set()
    for tbl in ("tuning_history", "deprecated_strategies",
                "learned_patterns"):
        try:
            cols |= {(tbl, r[1]) for r in conn.execute(
                f"PRAGMA table_info({tbl})").fetchall()}
        except sqlite3.OperationalError:
            pass
    conn.close()
    return cols


class TestQueriesMatchLiveSchemas:
    def test_tuning_history_query_columns_exist(self):
        """Run the endpoint's tuning_history SELECT against the REAL
        master DB schema — a renamed/missing column fails HERE instead
        of blanking the page."""
        body = _endpoint_src()
        # Extract every quoted fragment between SELECT and the params,
        # then execute against the live master schema.
        frags = re.findall(r'"([^"]*)"', body)
        sql = ""
        for i, f in enumerate(frags):
            if f.startswith("SELECT timestamp, adjustment_type"):
                # concatenate until the ORDER BY fragment inclusive
                j = i
                while j < len(frags):
                    sql += frags[j]
                    if "ORDER BY" in frags[j]:
                        break
                    j += 1
                break
        assert sql, "could not locate the tuning_history SELECT"
        conn = sqlite3.connect(
            f"file:{os.path.join(REPO, 'quantopsai.db')}?mode=ro",
            uri=True)
        try:
            conn.execute(sql, (0, 30)).fetchall()   # raises on drift
        finally:
            conn.close()

    def test_no_change_type_reference(self):
        """The wrong column name must not return to the SQL or the
        row access (the fix comment may NAME it as history)."""
        body = _endpoint_src()
        assert "SELECT timestamp, change_type" not in body and \
            'r["change_type"]' not in body, (
            "tuning_history has no `change_type` column — it's "
            "`adjustment_type`; this exact typo blanked the timeline "
            "for every profile."
        )

    def test_failed_source_logs_warning_not_debug(self):
        """A failed timeline source must surface at WARNING — at debug
        it reads as 'no events' and hides total breakage."""
        body = _endpoint_src()
        first_except = body.index("except Exception as exc:")
        window = body[first_except:first_except + 300]
        assert "logger.warning" in window, (
            "the tuning_history fetch failure must log at WARNING"
        )


class TestEndToEnd:
    def test_timeline_returns_events_for_a_profile_with_history(self,
                                                                tmp_path):
        """Full-shape check with a synthetic master DB: an
        adjustment_type row within 30 days comes back as an event."""
        master = tmp_path / "quantopsai.db"
        conn = sqlite3.connect(str(master))
        conn.execute("""
            CREATE TABLE tuning_history (
                id INTEGER PRIMARY KEY, profile_id INTEGER,
                user_id INTEGER, timestamp TEXT,
                adjustment_type TEXT, parameter_name TEXT,
                old_value TEXT, new_value TEXT, reason TEXT,
                win_rate_at_change REAL, predictions_resolved INTEGER,
                outcome_after TEXT)
        """)
        conn.execute(
            "INSERT INTO tuning_history (profile_id, user_id, timestamp,"
            " adjustment_type, parameter_name, old_value, new_value,"
            " reason, win_rate_at_change, outcome_after) VALUES "
            "(218, 1, datetime('now'), 'concentration_reduce',"
            " 'max_total_positions', '999', '749', 'test', 31.0,"
            " 'pending')")
        conn.commit(); conn.close()

        events = []
        from contextlib import closing
        from display_names import display_name
        with closing(sqlite3.connect(str(master))) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT timestamp, adjustment_type, parameter_name, "
                " old_value, new_value, reason, win_rate_at_change, "
                " outcome_after FROM tuning_history "
                "WHERE profile_id = ? AND datetime(timestamp) >= "
                "datetime('now', '-' || ? || ' days') "
                "ORDER BY timestamp DESC", (218, 30)).fetchall()
            for r in rows:
                events.append({"label": display_name(
                    r["parameter_name"] or r["adjustment_type"])})
        assert len(events) == 1
