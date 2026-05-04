"""Storage tests — schema, upsert with change detection, raw_filings."""

import sqlite3

import pytest

from biotechevents.store import (
    _apply_migrations,
    connect,
    counts_by_phase,
    init_db,
    insert_raw_filing,
    mark_raw_parsed,
    query_trials,
    recent_changes,
    upsert_trial,
)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "biotechevents.db"
    monkeypatch.setattr("biotechevents.store.DEFAULT_DB_PATH", str(db))
    init_db(str(db))
    return str(db)


class TestSchema:
    def test_tables_exist(self, tmp_db):
        conn = sqlite3.connect(tmp_db)
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        assert {"trials", "trial_changes", "pdufa_events",
                "raw_filings", "scrape_runs"}.issubset(names)

    def test_trials_has_parser_version(self, tmp_db):
        conn = sqlite3.connect(tmp_db)
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(trials)"
        ).fetchall()}
        conn.close()
        assert "parser_version" in cols


class TestMigrations:
    def test_idempotent(self, tmp_db):
        conn = sqlite3.connect(tmp_db)
        _apply_migrations(conn)
        _apply_migrations(conn)
        _apply_migrations(conn)
        conn.close()


class TestUpsertTrial:
    def _baseline(self):
        return {
            "nct_id": "NCT12345",
            "brief_title": "A study of XYZ",
            "sponsor_name": "Test Pharma",
            "sponsor_class": "INDUSTRY",
            "ticker": "TEST",
            "phase": "PHASE2",
            "overall_status": "RECRUITING",
            "primary_completion_date": "2025-12-31",
            "completion_date": "2026-06-30",
            "start_date": "2024-01-15",
            "last_updated": "2026-04-01",
            "enrollment_count": 100,
            "conditions": ["Cancer"],
            "interventions": ["Drug A"],
            "parser_version": "test-v1",
        }

    def test_first_insert_returns_is_new(self, tmp_db):
        with connect(tmp_db) as conn:
            result = upsert_trial(conn, **self._baseline())
            assert result["is_new"] is True
            assert result["changes"] == []

    def test_repeat_with_same_data_no_changes(self, tmp_db):
        with connect(tmp_db) as conn:
            upsert_trial(conn, **self._baseline())
            result = upsert_trial(conn, **self._baseline())
            assert result["is_new"] is False
            assert result["changes"] == []

    def test_phase_transition_detected(self, tmp_db):
        """The Phase 2 → Phase 3 transition is THE signal for biotech."""
        with connect(tmp_db) as conn:
            base = self._baseline()
            upsert_trial(conn, **base)
            base["phase"] = "PHASE3"
            result = upsert_trial(conn, **base)
            assert result["is_new"] is False
            assert len(result["changes"]) == 1
            change = result["changes"][0]
            assert change["field"] == "phase"
            assert change["old_value"] == "PHASE2"
            assert change["new_value"] == "PHASE3"

    def test_status_change_detected(self, tmp_db):
        with connect(tmp_db) as conn:
            base = self._baseline()
            upsert_trial(conn, **base)
            base["overall_status"] = "TERMINATED"
            result = upsert_trial(conn, **base)
            assert any(c["field"] == "overall_status" and
                       c["new_value"] == "TERMINATED"
                       for c in result["changes"])

    def test_completion_date_slip_detected(self, tmp_db):
        with connect(tmp_db) as conn:
            base = self._baseline()
            upsert_trial(conn, **base)
            # Trial slips by 6 months — this is bad signal
            base["primary_completion_date"] = "2026-06-30"
            result = upsert_trial(conn, **base)
            assert any(c["field"] == "primary_completion_date"
                       for c in result["changes"])

    def test_changes_recorded_in_trial_changes_table(self, tmp_db):
        with connect(tmp_db) as conn:
            base = self._baseline()
            upsert_trial(conn, **base)
            base["phase"] = "PHASE3"
            base["overall_status"] = "ACTIVE_NOT_RECRUITING"
            upsert_trial(conn, **base)
            rows = conn.execute(
                "SELECT field, old_value, new_value FROM trial_changes "
                "WHERE nct_id='NCT12345'"
            ).fetchall()
            assert len(rows) == 2

    def test_ticker_preserved_when_new_data_lacks_it(self, tmp_db):
        """If an upsert comes through with ticker=None but we have one
        stored, COALESCE preserves the existing value."""
        with connect(tmp_db) as conn:
            base = self._baseline()
            upsert_trial(conn, **base)
            # Re-upsert without ticker
            base["ticker"] = None
            upsert_trial(conn, **base)
            row = conn.execute(
                "SELECT ticker FROM trials WHERE nct_id='NCT12345'"
            ).fetchone()
            assert row["ticker"] == "TEST"


class TestRawFilings:
    def test_upsert_on_repeat(self, tmp_db):
        with connect(tmp_db) as conn:
            assert insert_raw_filing(
                conn, source="clinicaltrials", external_id="NCT1",
                content_type="json", payload="{}",
            ) is True
            # Second call updates
            assert insert_raw_filing(
                conn, source="clinicaltrials", external_id="NCT1",
                content_type="json", payload='{"v": 2}',
            ) is False

    def test_different_source_distinct(self, tmp_db):
        """A NCT id and an FDA event with the same external_id must
        not collide — the (source, external_id) UNIQUE allows this."""
        with connect(tmp_db) as conn:
            assert insert_raw_filing(
                conn, source="clinicaltrials", external_id="X1",
                content_type="json", payload="{}",
            ) is True
            assert insert_raw_filing(
                conn, source="fda", external_id="X1",
                content_type="json", payload="{}",
            ) is True

    def test_mark_parsed(self, tmp_db):
        with connect(tmp_db) as conn:
            insert_raw_filing(conn, "clinicaltrials", "NCT1", "json", "{}")
            mark_raw_parsed(conn, "clinicaltrials", "NCT1", "parsed")
            row = conn.execute(
                "SELECT parse_status FROM raw_filings WHERE external_id='NCT1'"
            ).fetchone()
            assert row["parse_status"] == "parsed"


class TestQueries:
    def test_query_by_phase(self, tmp_db):
        with connect(tmp_db) as conn:
            upsert_trial(conn, nct_id="A", brief_title="x",
                         sponsor_name=None, sponsor_class=None,
                         ticker=None, phase="PHASE3",
                         overall_status="RECRUITING",
                         primary_completion_date="2025-01-01",
                         completion_date=None, start_date=None,
                         last_updated=None, enrollment_count=None)
            upsert_trial(conn, nct_id="B", brief_title="y",
                         sponsor_name=None, sponsor_class=None,
                         ticker=None, phase="PHASE2",
                         overall_status="RECRUITING",
                         primary_completion_date="2025-01-01",
                         completion_date=None, start_date=None,
                         last_updated=None, enrollment_count=None)
            rows = query_trials(conn, phase="PHASE3")
            assert len(rows) == 1
            assert rows[0]["nct_id"] == "A"

    def test_recent_changes(self, tmp_db):
        with connect(tmp_db) as conn:
            base_args = dict(nct_id="A", brief_title="x",
                             sponsor_name="S", sponsor_class=None,
                             ticker=None,
                             overall_status="RECRUITING",
                             primary_completion_date="2025-01-01",
                             completion_date=None, start_date=None,
                             last_updated=None, enrollment_count=None)
            upsert_trial(conn, phase="PHASE2", **base_args)
            upsert_trial(conn, phase="PHASE3", **base_args)
            rows = recent_changes(conn, days=1)
            assert len(rows) == 1
            assert rows[0]["new_value"] == "PHASE3"
