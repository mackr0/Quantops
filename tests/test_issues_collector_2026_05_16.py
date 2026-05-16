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

    def _stub_collectors(self, journald_rows, altdata_rows, scrape_rows,
                          drift_rows=None):
        return patch.multiple(
            "issues_collector",
            _collect_journald=MagicMock(return_value=(journald_rows, None)),
            _collect_altdata_logs=MagicMock(return_value=(altdata_rows, None)),
            _collect_scrape_runs=MagicMock(return_value=(scrape_rows, None)),
            _collect_aggregate_drift=MagicMock(
                return_value=(drift_rows or [], None),
            ),
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
        ), patch(
            "issues_collector._collect_aggregate_drift",
            return_value=([], None),
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
            _collect_aggregate_drift=MagicMock(return_value=([], None)),
        ):
            c = issues_count()
        assert c["errors"] == 2
        assert c["warnings"] == 3
        assert c["total"] == 5


class TestLiveAggregateDrift:
    """2026-05-16: 123 outstanding drift items only surfaced when the
    profile-1 reconcile task fired during a scan cycle. Weekends were
    silent. Live drift check on /issues makes them visible always."""

    def setup_method(self):
        """Reset the 1h drift cache before each test so we re-fetch."""
        import issues_collector
        issues_collector._DRIFT_CACHE = {"ts": 0.0, "rows": [], "error": None}

    def test_drift_items_surface_as_error_rows(self):
        from issues_collector import _collect_aggregate_drift
        fake_audit = {
            "drift": [
                {"alpaca_account_id": "acct1", "symbol": "NXPI",
                 "journal_qty": 0.0, "broker_qty": -114.0,
                 "drift": -114.0, "category": "broker_orphan"},
                {"alpaca_account_id": "acct2", "symbol": "SM260618P00027500",
                 "journal_qty": 1.0, "broker_qty": 0.0,
                 "drift": -1.0, "category": "journal_phantom"},
            ],
            "by_account": {},
        }
        with patch("aggregate_audit.audit_aggregate_drift",
                   return_value=fake_audit):
            rows, err = _collect_aggregate_drift(24)
        assert err is None
        assert len(rows) == 2
        assert all(r["level"] == "ERROR" for r in rows)
        assert "broker_orphan" in rows[0]["message"]
        assert "NXPI" in rows[0]["message"]

    def test_drift_call_failure_surfaces_in_source_errors(self):
        """If aggregate_audit raises (Alpaca down, etc.), the failure
        of the live check itself must be visible — not silently hide
        the drift status. Returns empty rows + populated error."""
        from issues_collector import _collect_aggregate_drift
        with patch(
            "aggregate_audit.audit_aggregate_drift",
            side_effect=RuntimeError("Alpaca rate limit"),
        ):
            rows, err = _collect_aggregate_drift(24)
        assert rows == []
        assert err is not None
        assert "RuntimeError" in err and "Alpaca rate limit" in err

    def test_drift_cache_avoids_repeated_alpaca_calls(self):
        """1h cache: second call within window should NOT re-invoke
        aggregate_audit (Alpaca rate-limit pressure on /issues
        reloads)."""
        from issues_collector import _collect_aggregate_drift
        fake_audit = {"drift": [], "by_account": {}}
        with patch("aggregate_audit.audit_aggregate_drift",
                   return_value=fake_audit) as m:
            _collect_aggregate_drift(24)
            _collect_aggregate_drift(24)
        assert m.call_count == 1, (
            "Second call within 1h cache window must NOT re-invoke"
        )


class TestLogTimestampExtraction:
    """User caught (2026-05-16) that altdata log entries had blank
    `last_seen` so /issues couldn't tell ancient errors from fresh
    ones. Parse the timestamp prefix off each line."""

    def test_python_logging_format_parsed(self):
        from issues_collector import _extract_log_timestamp
        line = "2026-05-16 06:08:37,844 [ERROR] yfinance: $BRK.B"
        assert _extract_log_timestamp(line) == "2026-05-16T06:08:37"

    def test_iso_z_format_parsed(self):
        from issues_collector import _extract_log_timestamp
        line = "2026-05-16T06:08:37Z [WARNING] something"
        assert _extract_log_timestamp(line) == "2026-05-16T06:08:37"

    def test_no_timestamp_returns_empty(self):
        from issues_collector import _extract_log_timestamp
        assert _extract_log_timestamp("[ERROR] no ts here") == ""
        assert _extract_log_timestamp("") == ""

    def test_collected_altdata_row_carries_timestamp(self, tmp_path):
        """End-to-end: log file with a real timestamp, collector
        extracts it into the row's `timestamp` field. Pre-2026-05-16
        this was empty for every altdata row."""
        from issues_collector import _collect_altdata_logs
        # Build a small log fixture in a temp dir; point collector at it.
        log = tmp_path / "altdata-20260516.log"
        log.write_text(
            "2026-05-16 06:08:37,844 [ERROR] yfinance: $BRK.B test\n"
            "2026-05-16 06:09:12,001 [WARNING] something else\n"
        )
        import issues_collector
        orig = issues_collector._altdata_log_paths
        issues_collector._altdata_log_paths = lambda h: [str(log)]
        try:
            rows, err = _collect_altdata_logs(24)
        finally:
            issues_collector._altdata_log_paths = orig
        assert err is None
        assert len(rows) == 2
        ts_values = sorted(r["timestamp"] for r in rows)
        assert ts_values == ["2026-05-16T06:08:37", "2026-05-16T06:09:12"], (
            "Each row must carry the per-line timestamp, not empty"
        )


class TestLiveDriftIsLiveSnapshot:
    """Drift rows shouldn't fake a moment-in-time timestamp — they
    represent persistent state, not new events. Carry an
    `is_live_snapshot` flag so the template renders 'live snapshot'
    instead of 'happened just now'."""

    def setup_method(self):
        import issues_collector
        issues_collector._DRIFT_CACHE = {"ts": 0.0, "rows": [], "error": None}

    def test_drift_row_marked_live_snapshot(self):
        from issues_collector import _collect_aggregate_drift
        fake_audit = {
            "drift": [
                {"alpaca_account_id": "acct1", "symbol": "NXPI",
                 "journal_qty": 0.0, "broker_qty": -114.0,
                 "drift": -114.0, "category": "broker_orphan"},
            ],
            "by_account": {},
        }
        with patch("aggregate_audit.audit_aggregate_drift",
                   return_value=fake_audit):
            rows, _ = _collect_aggregate_drift(24)
        assert rows[0]["is_live_snapshot"] is True, (
            "drift rows must be marked as live snapshots so the UI "
            "can render 'live snapshot' rather than a fake timestamp"
        )
        assert rows[0]["timestamp"] == "", (
            "drift rows must NOT carry a fake datetime.utcnow() — "
            "the underlying state may have existed for days"
        )

    def test_grouping_preserves_live_snapshot_flag(self):
        from issues_collector import collect_issues
        fake_audit = {
            "drift": [
                {"alpaca_account_id": "acct1", "symbol": "NXPI",
                 "journal_qty": 0.0, "broker_qty": -114.0,
                 "drift": -114.0, "category": "broker_orphan"},
            ],
            "by_account": {},
        }
        with patch.multiple(
            "issues_collector",
            _collect_journald=MagicMock(return_value=([], None)),
            _collect_altdata_logs=MagicMock(return_value=([], None)),
            _collect_scrape_runs=MagicMock(return_value=([], None)),
        ), patch("aggregate_audit.audit_aggregate_drift",
                 return_value=fake_audit):
            out = collect_issues()
        assert out["total_groups"] == 1
        assert out["groups"][0]["is_live_snapshot"] is True


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

        # Point the collector at this fixture instead of /opt/...
        monkeypatch.setattr(
            "issues_collector._ALTDATA_DBS",
            [("edgar_form4", str(db_path))],
        )
        rows, err = _collect_scrape_runs(since_hours=24)
        assert err is None
        assert len(rows) == 2  # ok row excluded
        levels = {r["level"] for r in rows}
        assert "ERROR" in levels  # failed
        assert "WARNING" in levels  # ok_with_errors

    def test_json_error_format_surfaces_per_item_detail(
        self, tmp_path, monkeypatch,
    ):
        """2026-05-16 addition: when scrape_runs.error is a JSON
        blob (per-item error persistence), each failed item should
        surface as its OWN /issues row so the operator sees WHICH
        ticker failed and WHY."""
        import json as _json
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
        err_json = _json.dumps({
            "summary": "3 ticker error(s)",
            "items": [
                {"label": "ANSS", "reason": "no CIK mapping"},
                {"label": "BITF", "reason": "no CIK mapping"},
                {"label": "CEIX", "reason": "no CIK mapping"},
            ],
        })
        conn.execute(
            "INSERT INTO scrape_runs (source, started_at, status, error) "
            "VALUES ('daily:525', datetime('now'), 'ok_with_errors', ?)",
            (err_json,),
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr(
            "issues_collector._ALTDATA_DBS",
            [("edgar_form4", str(db_path))],
        )
        rows, err = _collect_scrape_runs(since_hours=24)
        assert err is None
        # 1 summary row + 3 per-item rows
        assert len(rows) == 4
        per_item = [r for r in rows if "ANSS" in r["message"]
                    or "BITF" in r["message"] or "CEIX" in r["message"]]
        assert len(per_item) == 3
        assert all("no CIK mapping" in r["message"] for r in per_item)

    def test_legacy_plain_text_error_still_works(
        self, tmp_path, monkeypatch,
    ):
        """Pre-2026-05-16 scrape_runs.error was a plain string like
        '63 ticker error(s)'. Must still render correctly — the JSON
        decode is opt-in, plain text is the default."""
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
            "VALUES ('daily:525', datetime('now'), 'ok_with_errors', "
            "        '63 ticker error(s)')"
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr(
            "issues_collector._ALTDATA_DBS",
            [("edgar_form4", str(db_path))],
        )
        rows, err = _collect_scrape_runs(since_hours=24)
        assert err is None
        assert len(rows) == 1, (
            "Plain-text legacy error: ONE summary row, NO per-item "
            "breakdown (decode opt-in via JSON)"
        )
        assert rows[0]["level"] == "WARNING"
        assert "63 ticker error(s)" in rows[0]["message"]
