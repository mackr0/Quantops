"""Storage layer tests — schema, CRUD, idempotency."""

import sqlite3
from pathlib import Path

import pytest

from edgar13f.store import (
    _apply_migrations,
    connect,
    counts_by_period,
    init_db,
    insert_filing,
    insert_holding,
    insert_raw_filing,
    mark_raw_parsed,
    query_holdings,
    upsert_filer,
)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "edgar13f.db"
    monkeypatch.setattr("edgar13f.store.DEFAULT_DB_PATH", str(db))
    init_db(str(db))
    return str(db)


class TestSchema:
    def test_tables_exist(self, tmp_db):
        conn = sqlite3.connect(tmp_db)
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        assert {"filers", "filings", "holdings", "raw_filings",
                "scrape_runs"}.issubset(names)

    def test_holdings_has_parser_version(self, tmp_db):
        """Contract: every parsed row must be taggable with parser_version
        so a future parser upgrade can re-process specific rows."""
        conn = sqlite3.connect(tmp_db)
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(holdings)"
        ).fetchall()}
        conn.close()
        assert "parser_version" in cols

    def test_raw_filings_has_parse_status(self, tmp_db):
        conn = sqlite3.connect(tmp_db)
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(raw_filings)"
        ).fetchall()}
        conn.close()
        assert {"parse_status", "parse_error"}.issubset(cols)


class TestMigrationIdempotency:
    def test_double_migration_no_crash(self, tmp_db):
        conn = sqlite3.connect(tmp_db)
        _apply_migrations(conn)
        _apply_migrations(conn)   # second call should not raise
        conn.close()


class TestFilerUpsert:
    def test_insert_new(self, tmp_db):
        with connect(tmp_db) as conn:
            upsert_filer(conn, cik="0001067983",
                         name="Berkshire Hathaway Inc",
                         filer_type="conglomerate")
            row = conn.execute(
                "SELECT * FROM filers WHERE cik='0001067983'"
            ).fetchone()
            assert row["name"] == "Berkshire Hathaway Inc"
            assert row["filer_type"] == "conglomerate"

    def test_update_preserves_existing_fields(self, tmp_db):
        with connect(tmp_db) as conn:
            upsert_filer(conn, cik="C", name="Original", filer_type="hedge_fund")
            # Update with no filer_type — should preserve "hedge_fund"
            upsert_filer(conn, cik="C", name="Renamed")
            row = conn.execute("SELECT * FROM filers WHERE cik='C'").fetchone()
            assert row["name"] == "Renamed"
            assert row["filer_type"] == "hedge_fund"


class TestFilings:
    def test_insert_new(self, tmp_db):
        with connect(tmp_db) as conn:
            upsert_filer(conn, cik="X", name="X")
            assert insert_filing(
                conn, accession_number="A", cik="X",
                period_of_report="2025-09-30", filed_date="2025-11-15",
            ) is True

    def test_duplicate_returns_false(self, tmp_db):
        with connect(tmp_db) as conn:
            upsert_filer(conn, cik="X", name="X")
            insert_filing(conn, accession_number="A", cik="X",
                          period_of_report="2025-09-30",
                          filed_date="2025-11-15")
            assert insert_filing(
                conn, accession_number="A", cik="X",
                period_of_report="2025-09-30", filed_date="2025-11-15",
            ) is False


class TestHoldings:
    def _filing(self, conn, accession="A", cik="X"):
        upsert_filer(conn, cik=cik, name=cik)
        insert_filing(conn, accession_number=accession, cik=cik,
                      period_of_report="2025-09-30",
                      filed_date="2025-11-15")

    def test_insert_new(self, tmp_db):
        with connect(tmp_db) as conn:
            self._filing(conn)
            assert insert_holding(
                conn, accession_number="A", cusip="037833100",
                company_name="Apple Inc",
                class_title="COM", ticker="AAPL",
                shares=1000000, value_usd=200_000_000,
                parser_version="13f-xml-v1",
            ) is True

    def test_duplicate_returns_false(self, tmp_db):
        with connect(tmp_db) as conn:
            self._filing(conn)
            insert_holding(conn, accession_number="A", cusip="037833100",
                           company_name="Apple Inc", class_title="COM")
            # Same (accession, cusip, class, put_call) → dup
            assert insert_holding(
                conn, accession_number="A", cusip="037833100",
                company_name="Apple Inc", class_title="COM",
            ) is False

    def test_different_class_not_dup(self, tmp_db):
        with connect(tmp_db) as conn:
            self._filing(conn)
            # Berkshire files two Apple entries (different share classes)
            assert insert_holding(
                conn, accession_number="A", cusip="037833100",
                company_name="Apple Inc", class_title="COM",
            ) is True
            assert insert_holding(
                conn, accession_number="A", cusip="037833100",
                company_name="Apple Inc", class_title="CLASS A",
            ) is True

    def test_query_by_ticker(self, tmp_db):
        with connect(tmp_db) as conn:
            self._filing(conn)
            insert_holding(conn, accession_number="A", cusip="037833100",
                           company_name="Apple Inc", ticker="AAPL",
                           class_title="COM", value_usd=100)
            rows = query_holdings(conn, ticker="AAPL")
            assert len(rows) == 1
            assert rows[0]["ticker"] == "AAPL"


class TestRawFilings:
    def test_insert_xml(self, tmp_db):
        with connect(tmp_db) as conn:
            assert insert_raw_filing(
                conn, accession_number="A-1", cik="X",
                filing_type="13F-HR", content_type="xml",
                payload="<xml/>",
            ) is True

    def test_upsert_on_repeat(self, tmp_db):
        with connect(tmp_db) as conn:
            insert_raw_filing(conn, accession_number="A-1", cik="X",
                              filing_type="13F-HR", content_type="xml",
                              payload="<v1/>")
            # Second call → update, returns False
            assert insert_raw_filing(
                conn, accession_number="A-1", cik="X",
                filing_type="13F-HR", content_type="xml", payload="<v2/>",
            ) is False
            row = conn.execute(
                "SELECT payload_text FROM raw_filings WHERE accession_number='A-1'"
            ).fetchone()
            assert row[0] == "<v2/>"

    def test_mark_parsed_lifecycle(self, tmp_db):
        with connect(tmp_db) as conn:
            insert_raw_filing(conn, "A-1", "X", "13F-HR", "xml", "<v/>")
            mark_raw_parsed(conn, "A-1", "parsed")
            row = conn.execute(
                "SELECT parse_status, parse_error FROM raw_filings"
            ).fetchone()
            assert row[0] == "parsed"
            assert row[1] is None

    def test_mark_error_captures_message(self, tmp_db):
        with connect(tmp_db) as conn:
            insert_raw_filing(conn, "A-1", "X", "13F-HR", "xml", "<v/>")
            mark_raw_parsed(conn, "A-1", "parse_error", "bad XML")
            row = conn.execute(
                "SELECT parse_status, parse_error FROM raw_filings"
            ).fetchone()
            # row is a sqlite3.Row — index by column name
            assert row["parse_status"] == "parse_error"
            assert row["parse_error"] == "bad XML"
