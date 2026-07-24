"""The "today (ET)" cost readers must draw the day boundary at the TRUE
ET midnight, not at UTC midnight of the ET-labeled date.

Both ledger tables (`ai_cost_ledger`, `ai_shadow_calls`) stamp rows with
SQLite's `datetime('now')` — **UTC**, `YYYY-MM-DD HH:MM:SS`. The
pre-2026-07-24 readers compared those UTC stamps against the bare ET
date string (`timestamp >= '2026-07-23'`), i.e. UTC midnight — a window
that opens 4–5 hours early (8 PM the previous ET evening), so
late-evening spend counted toward the NEXT ET day's dashboard line.
Everything displayed is ET; the boundary must be ET midnight converted
to UTC. Fix: `_utc_start_of_et_today()`, shared by `by_model_today`,
`shadow_by_model_today`, and `spend_summary`'s today bucket.
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ai_cost_ledger import (  # noqa: E402
    _utc_start_of_et_today, by_model_today, shadow_by_model_today,
    spend_summary,
)

UTC_FMT = "%Y-%m-%d %H:%M:%S"


class TestHelper:
    def test_edt_midnight_converts_to_0400_utc(self):
        """July (EDT, UTC-4): 21:10 ET Jul 23 == 01:10 UTC Jul 24.
        Today-ET is still Jul 23 → boundary is Jul 23 04:00 UTC."""
        now = datetime(2026, 7, 24, 1, 10, tzinfo=timezone.utc)
        assert _utc_start_of_et_today(now) == "2026-07-23 04:00:00"

    def test_est_midnight_converts_to_0500_utc(self):
        """January (EST, UTC-5): 22:00 ET Jan 14 == 03:00 UTC Jan 15
        → boundary is Jan 14 05:00 UTC."""
        now = datetime(2026, 1, 15, 3, 0, tzinfo=timezone.utc)
        assert _utc_start_of_et_today(now) == "2026-01-14 05:00:00"

    def test_midday_is_same_et_and_utc_date(self):
        """15:00 UTC Jul 23 == 11:00 ET Jul 23 → Jul 23 04:00 UTC."""
        now = datetime(2026, 7, 23, 15, 0, tzinfo=timezone.utc)
        assert _utc_start_of_et_today(now) == "2026-07-23 04:00:00"


def _mk_op_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE ai_cost_ledger ("
        " id INTEGER PRIMARY KEY,"
        " timestamp TEXT NOT NULL DEFAULT (datetime('now')),"
        " provider TEXT, model TEXT, input_tokens INTEGER,"
        " output_tokens INTEGER, purpose TEXT,"
        " estimated_cost_usd REAL, call_id TEXT,"
        " cached_tokens INTEGER)"
    )
    conn.commit()
    return conn


def _mk_shadow_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE ai_shadow_calls ("
        " id INTEGER PRIMARY KEY,"
        " timestamp TEXT NOT NULL DEFAULT (datetime('now')),"
        " provider TEXT, model TEXT, cost_usd REAL, error TEXT)"
    )
    conn.commit()
    return conn


def _boundary():
    """The live boundary the readers will use, as an aware datetime."""
    return datetime.strptime(
        _utc_start_of_et_today(), UTC_FMT).replace(tzinfo=timezone.utc)


class TestReadersHonorTheBoundary:
    """Rows one second either side of the REAL helper output: before →
    excluded, after → included. DST-proof (boundary computed live)."""

    def test_by_model_today_excludes_prior_et_evening(self, tmp_path):
        db = str(tmp_path / "op.db")
        conn = _mk_op_db(db)
        b = _boundary()
        before = (b - timedelta(seconds=1)).strftime(UTC_FMT)
        after = (b + timedelta(seconds=1)).strftime(UTC_FMT)
        conn.execute(
            "INSERT INTO ai_cost_ledger (timestamp, provider, model,"
            " estimated_cost_usd) VALUES (?, 'google', 'g-flash', 5.0)",
            (before,))
        conn.execute(
            "INSERT INTO ai_cost_ledger (timestamp, provider, model,"
            " estimated_cost_usd) VALUES (?, 'google', 'g-flash', 1.0)",
            (after,))
        conn.commit(); conn.close()
        out = by_model_today(db)
        assert len(out) == 1
        assert out[0]["calls"] == 1, (
            "The prior-ET-evening row leaked into today — the UTC-"
            "midnight window is back."
        )
        assert out[0]["usd"] == 1.0

    def test_shadow_by_model_today_excludes_prior_et_evening(
            self, tmp_path):
        db = str(tmp_path / "sh.db")
        conn = _mk_shadow_db(db)
        b = _boundary()
        before = (b - timedelta(seconds=1)).strftime(UTC_FMT)
        after = (b + timedelta(seconds=1)).strftime(UTC_FMT)
        conn.execute(
            "INSERT INTO ai_shadow_calls (timestamp, provider, model,"
            " cost_usd) VALUES (?, 'anthropic', 'haiku', 5.0)", (before,))
        conn.execute(
            "INSERT INTO ai_shadow_calls (timestamp, provider, model,"
            " cost_usd) VALUES (?, 'anthropic', 'haiku', 0.25)", (after,))
        conn.commit(); conn.close()
        out = shadow_by_model_today(db)
        assert len(out) == 1
        assert out[0]["calls"] == 1
        assert out[0]["usd"] == 0.25

    def test_spend_summary_today_excludes_prior_et_evening(
            self, tmp_path):
        db = str(tmp_path / "ss.db")
        conn = _mk_op_db(db)
        b = _boundary()
        before = (b - timedelta(seconds=1)).strftime(UTC_FMT)
        after = (b + timedelta(seconds=1)).strftime(UTC_FMT)
        for ts, usd in ((before, 5.0), (after, 1.5)):
            conn.execute(
                "INSERT INTO ai_cost_ledger (timestamp, provider, model,"
                " input_tokens, output_tokens, estimated_cost_usd)"
                " VALUES (?, 'google', 'g-flash', 10, 5, ?)", (ts, usd))
        conn.commit(); conn.close()
        today = spend_summary(db)["today"]
        assert today["calls"] == 1
        assert today["usd"] == 1.5

    def test_default_stamped_row_counts_as_today(self, tmp_path):
        """A row written NOW via the table default (the production
        write path) is always inside today's ET day."""
        db = str(tmp_path / "now.db")
        conn = _mk_shadow_db(db)
        conn.execute(
            "INSERT INTO ai_shadow_calls (provider, model, cost_usd)"
            " VALUES ('anthropic', 'haiku', 0.01)")
        conn.commit(); conn.close()
        out = shadow_by_model_today(db)
        assert len(out) == 1 and out[0]["calls"] == 1


class TestStructural:
    def test_no_bare_et_date_compare_left(self):
        """The bug shape — an ET DATE string (no time) used as the
        timestamp lower bound — must not return to ai_cost_ledger.py.
        All three today-readers route through _utc_start_of_et_today."""
        src = open(os.path.join(
            os.path.dirname(__file__), os.pardir,
            "ai_cost_ledger.py")).read()
        assert not re.search(
            r"strftime\(\s*[\"']%Y-%m-%d[\"']\s*\)", src), (
            "A bare ET date string is being formatted in "
            "ai_cost_ledger.py — the UTC-midnight 'today' window is "
            "creeping back. Use _utc_start_of_et_today()."
        )
        assert src.count("_utc_start_of_et_today()") >= 3, (
            "All three today-readers (by_model_today, "
            "shadow_by_model_today, spend_summary) must use the "
            "shared ET-midnight boundary."
        )
