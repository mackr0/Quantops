"""Issues collector tests — every silent failure surface must be
discoverable in the new /issues page (built 2026-05-16 in response to
the user's "no more hiding this shit" mandate).

What this pins:
  - Signature reducer collapses dynamic bits (UUIDs, OCC symbols,
    timestamps, profile_<n>) so 1000+ spam events from the same root
    cause appear as ONE group with `occurrences=1000`, not 1000 rows.
  - Groups sort with ERROR/CRITICAL above WARNING.
  - Collector-source failures (journald missing, DB unreadable) end
    up in `summary.source_errors` instead of crashing the page —
    the failure of the collector itself must be visible.
  - issues_count returns the badge-shape the nav JS expects.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


from issues_collector import (  # noqa: E402
    _signature,
    collect_issues,
    issues_count,
)


class TestSignatureReducer:
    def test_uuid_normalized(self):
        s = _signature(
            "shadow eval: call_id=550e8400-e29b-41d4-a716-446655440000 failed"
        )
        assert "550e8400" not in s
        assert "<uuid>" in s

    def test_occ_symbol_normalized(self):
        s = _signature("Option-premium fetch returned 0 for CWAN260612C00026000")
        assert "CWAN260612C00026000" not in s
        assert "<occ>" in s

    def test_profile_id_normalized(self):
        a = _signature("update_fills failed for profile_7")
        b = _signature("update_fills failed for profile_11")
        assert a == b

    def test_iso_timestamp_normalized(self):
        s = _signature("event at 2026-05-16T20:06:04.123Z fired")
        assert "2026-05-16" not in s
        assert "<ts>" in s

    def test_distinct_root_causes_stay_distinct(self):
        """Negative case: two genuinely different errors must NOT
        collapse into the same signature (would hide a real bug)."""
        a = _signature("VIX from SPY options unavailable")
        b = _signature("Option-premium fetch returned 0 for CWAN260612C00026000")
        assert a != b


class TestCollectorReturnShape:
    """Run the collector with all source-collectors stubbed; pin the
    grouping + sort behavior."""

    def _stub_collectors(self, journald_rows, altdata_rows, scrape_rows):
        return patch.multiple(
            "issues_collector",
            _collect_journald=MagicMock(return_value=(journald_rows, None)),
            _collect_altdata_logs=MagicMock(return_value=(altdata_rows, None)),
            _collect_scrape_runs=MagicMock(return_value=(scrape_rows, None)),
        )

    def test_groups_spam_events_into_one_row(self):
        # Vary the OCC root letters so each event has a DIFFERENT
        # OCC symbol but the same root cause. The regex normalizes
        # OCC to <occ> so all 50 collapse into one group.
        roots = [
            ("AAA", "BBB", "CCC", "DDD", "EEE")[i % 5]
            for i in range(50)
        ]
        spam = [
            {"source": "quantopsai-web",
             "level": "WARNING",
             "message": (
                 f"Option-premium fetch returned 0 for {roots[i]}260612C00026000"
             ),
             "timestamp": "2026-05-16T20:0{}:00".format(i % 10)}
            for i in range(50)
        ]
        with self._stub_collectors(spam, [], []):
            out = collect_issues(since_hours=24)
        assert out["total_events"] == 50
        assert out["total_groups"] == 1, (
            "Signature reducer must collapse 50 OCC-only-different "
            "spam events into ONE group"
        )
        assert out["groups"][0]["occurrences"] == 50

    def test_errors_sort_above_warnings(self):
        rows = [
            {"source": "quantopsai", "level": "WARNING",
             "message": "minor warn", "timestamp": "2026-05-16T20:00:00"},
            {"source": "quantopsai", "level": "ERROR",
             "message": "real problem", "timestamp": "2026-05-16T19:00:00"},
            {"source": "quantopsai", "level": "CRITICAL",
             "message": "fire", "timestamp": "2026-05-16T18:00:00"},
        ]
        with self._stub_collectors(rows, [], []):
            out = collect_issues(since_hours=24)
        levels = [g["level"] for g in out["groups"]]
        assert levels == ["CRITICAL", "ERROR", "WARNING"], (
            "Errors must sort above warnings; older critical comes "
            "before newer warning"
        )

    def test_level_filter_narrows_results(self):
        rows = [
            {"source": "x", "level": "WARNING",
             "message": "w", "timestamp": "2026-05-16T20:00:00"},
            {"source": "x", "level": "ERROR",
             "message": "e", "timestamp": "2026-05-16T20:00:00"},
        ]
        with self._stub_collectors(rows, [], []):
            out = collect_issues(level_filter="ERROR,CRITICAL")
        assert out["total_groups"] == 1
        assert out["groups"][0]["level"] == "ERROR"

    def test_collector_source_errors_propagate(self):
        """The collector itself failing must be visible — not swallowed."""
        with patch(
            "issues_collector._collect_journald",
            return_value=([], "journalctl exit 1: permission denied"),
        ), patch(
            "issues_collector._collect_altdata_logs", return_value=([], None),
        ), patch(
            "issues_collector._collect_scrape_runs", return_value=([], None),
        ):
            out = collect_issues()
        assert any(
            "journald" in e and "permission denied" in e
            for e in out["source_errors"]
        ), (
            "Collector-source errors must surface in source_errors "
            "so the failure of the issues page itself is visible"
        )


class TestIssuesCount:
    def test_count_shape_for_nav_badge(self):
        rows = [
            {"source": "x", "level": "ERROR",
             "message": "e1", "timestamp": "2026-05-16T20:00:00"},
            {"source": "x", "level": "ERROR",
             "message": "e2", "timestamp": "2026-05-16T20:00:00"},
            {"source": "x", "level": "WARNING",
             "message": "w", "timestamp": "2026-05-16T20:00:00"},
            {"source": "x", "level": "WARNING",
             "message": "w2", "timestamp": "2026-05-16T20:00:00"},
            {"source": "x", "level": "WARNING",
             "message": "w3", "timestamp": "2026-05-16T20:00:00"},
        ]
        with patch.multiple(
            "issues_collector",
            _collect_journald=MagicMock(return_value=(rows, None)),
            _collect_altdata_logs=MagicMock(return_value=([], None)),
            _collect_scrape_runs=MagicMock(return_value=([], None)),
        ):
            c = issues_count()
        assert c["errors"] == 2
        assert c["warnings"] == 3
        assert c["total"] == 5


class TestScrapeRunsCollector:
    """Real DB integration for the scrape_runs source."""

    def test_failed_runs_surface_as_errors(self, tmp_path, monkeypatch):
        from issues_collector import _collect_scrape_runs
        db_path = tmp_path / "edgar_form4.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE scrape_runs ("
            " id INTEGER PRIMARY KEY,"
            " source TEXT, started_at TEXT, finished_at TEXT,"
            " status TEXT, rows_inserted INTEGER, rows_seen INTEGER,"
            " error TEXT)"
        )
        conn.execute(
            "INSERT INTO scrape_runs (source, started_at, status, error) "
            "VALUES ('daily:525', datetime('now'), 'failed', 'rate limited')"
        )
        conn.execute(
            "INSERT INTO scrape_runs (source, started_at, status, error) "
            "VALUES ('daily:525', datetime('now'), 'ok_with_errors', "
            "        '63 ticker error(s)')"
        )
        conn.execute(
            "INSERT INTO scrape_runs (source, started_at, status, error) "
            "VALUES ('daily:525', datetime('now'), 'ok', NULL)"
        )
        conn.commit()
        conn.close()

        # Point the collector at our fixture instead of /opt/...
        monkeypatch.setattr(
            "issues_collector._ALTDATA_DBS",
            [("edgar_form4", str(db_path))],
        )
        rows, err = _collect_scrape_runs(since_hours=24)
        assert err is None
        assert len(rows) == 2, (
            "Both 'failed' and 'ok_with_errors' must surface; 'ok' must not"
        )
        levels = {r["level"] for r in rows}
        assert "ERROR" in levels  # the failed row
        assert "WARNING" in levels  # the ok_with_errors row
