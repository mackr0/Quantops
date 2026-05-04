"""Tests for store.py — schema + CRUD + idempotency.

Uses a tmp_path DB so tests are hermetic.
"""

import sqlite3
from pathlib import Path

import pytest

from congresstrades.store import (
    connect,
    counts_by_chamber,
    init_db,
    insert_raw_filing,
    insert_trade,
    mark_raw_filing_parsed,
    query_trades,
    recent_runs,
    start_run,
    finish_run,
    _apply_migrations,
)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Provide a temp DB path and patch the DEFAULT_DB_PATH so store uses it."""
    db = tmp_path / "congress.db"
    monkeypatch.setattr("congresstrades.store.DEFAULT_DB_PATH", str(db))
    init_db(str(db))
    return str(db)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class TestSchema:
    def test_tables_exist(self, tmp_db):
        conn = sqlite3.connect(tmp_db)
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        assert {"trades", "raw_filings", "scrape_runs"}.issubset(names)

    def test_trades_has_expected_columns(self, tmp_db):
        """Contract test — these columns are referenced by scraper code.
        Adding new columns is fine. Removing or renaming breaks the code."""
        expected = {
            "id", "chamber", "member_name", "member_state", "member_party",
            "filing_doc_id", "filing_date", "transaction_date",
            "ticker", "asset_description", "asset_type",
            "transaction_type",
            "amount_range", "amount_low", "amount_high",
            "owner", "source_url", "raw_json", "parser_version", "fetched_at",
        }
        conn = sqlite3.connect(tmp_db)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()}
        conn.close()
        missing = expected - cols
        assert not missing, f"trades table missing columns: {missing}"

    def test_raw_filings_has_expected_columns(self, tmp_db):
        expected = {
            "id", "chamber", "filing_doc_id", "member_name", "filing_type",
            "source_url", "content_type", "payload_text", "payload_blob",
            "filed_on", "fetched_at", "parse_status", "parse_error",
        }
        conn = sqlite3.connect(tmp_db)
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(raw_filings)"
        ).fetchall()}
        conn.close()
        missing = expected - cols
        assert not missing, f"raw_filings table missing columns: {missing}"


# ---------------------------------------------------------------------------
# Migrations — idempotent
# ---------------------------------------------------------------------------

class TestMigrations:
    def test_migrations_idempotent(self, tmp_db):
        """Running _apply_migrations twice must not raise."""
        conn = sqlite3.connect(tmp_db)
        _apply_migrations(conn)  # first run already happened in init_db
        _apply_migrations(conn)  # second run
        _apply_migrations(conn)  # third run
        conn.close()

    def test_migrations_on_legacy_schema(self, tmp_path):
        """Simulate an old DB missing the new columns — migration adds them."""
        db = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(db))
        conn.executescript("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chamber TEXT NOT NULL,
                member_name TEXT NOT NULL,
                filing_doc_id TEXT,
                transaction_date TEXT,
                asset_description TEXT NOT NULL,
                amount_range TEXT,
                fetched_at TEXT NOT NULL,
                UNIQUE (chamber, member_name, filing_doc_id, transaction_date, asset_description, amount_range)
            );
        """)
        conn.commit()
        _apply_migrations(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()}
        conn.close()
        assert "asset_type" in cols
        assert "parser_version" in cols


# ---------------------------------------------------------------------------
# insert_trade
# ---------------------------------------------------------------------------

class TestInsertTrade:
    def _sample(self, **overrides):
        base = {
            "chamber": "house",
            "member_name": "Jane Doe",
            "filing_doc_id": "12345",
            "transaction_date": "2025-01-15",
            "asset_description": "Apple Inc. (AAPL)",
            "ticker": "AAPL",
            "transaction_type": "buy",
            "amount_range": "$1,001 - $15,000",
            "amount_low": 1001, "amount_high": 15000,
            "parser_version": "test-v1",
        }
        base.update(overrides)
        return base

    def test_inserts_new_trade(self, tmp_db):
        with connect(tmp_db) as conn:
            assert insert_trade(conn, self._sample()) is True

    def test_duplicate_returns_false(self, tmp_db):
        with connect(tmp_db) as conn:
            assert insert_trade(conn, self._sample()) is True
            # Same row again — UNIQUE kicks in, returns False
            assert insert_trade(conn, self._sample()) is False

    def test_different_amount_range_not_dup(self, tmp_db):
        with connect(tmp_db) as conn:
            insert_trade(conn, self._sample())
            # Same member+date+asset but different amount — counts as separate
            assert insert_trade(conn, self._sample(
                amount_range="$15,001 - $50,000")) is True

    def test_parser_version_persists(self, tmp_db):
        with connect(tmp_db) as conn:
            insert_trade(conn, self._sample(parser_version="senate-html-v1"))
            rows = query_trades(conn)
            assert rows[0]["parser_version"] == "senate-html-v1"


# ---------------------------------------------------------------------------
# Raw filings
# ---------------------------------------------------------------------------

class TestRawFilings:
    def test_insert_html_payload(self, tmp_db):
        with connect(tmp_db) as conn:
            new = insert_raw_filing(
                conn, chamber="senate", filing_doc_id="abc",
                content_type="html", payload="<html>test</html>",
                source_url="https://example.test/abc",
                member_name="Test Senator",
                filing_type="electronic", filed_on="2026-04-24",
            )
        assert new is True

    def test_upsert_returns_false(self, tmp_db):
        with connect(tmp_db) as conn:
            insert_raw_filing(conn, "senate", "abc", "html", "<v1/>")
            # Second call with different payload — updates not duplicates
            updated = insert_raw_filing(conn, "senate", "abc", "html", "<v2/>")
            assert updated is False
            row = conn.execute(
                "SELECT payload_text FROM raw_filings WHERE filing_doc_id='abc'"
            ).fetchone()
            assert row[0] == "<v2/>"

    def test_pdf_blob_payload(self, tmp_db):
        with connect(tmp_db) as conn:
            insert_raw_filing(conn, "house", "pdf-1", "pdf", b"%PDF-1.4\n...")
            row = conn.execute(
                "SELECT content_type, payload_blob, payload_text FROM raw_filings "
                "WHERE filing_doc_id='pdf-1'"
            ).fetchone()
        assert row[0] == "pdf"
        assert row[1] == b"%PDF-1.4\n..."
        assert row[2] is None

    def test_mark_parsed_lifecycle(self, tmp_db):
        with connect(tmp_db) as conn:
            insert_raw_filing(conn, "senate", "xyz", "html", "<v/>")
            mark_raw_filing_parsed(conn, "senate", "xyz", status="parsed")
            row = conn.execute(
                "SELECT parse_status, parse_error FROM raw_filings "
                "WHERE filing_doc_id='xyz'"
            ).fetchone()
        assert row[0] == "parsed"
        assert row[1] is None

    def test_mark_error_with_message(self, tmp_db):
        with connect(tmp_db) as conn:
            insert_raw_filing(conn, "senate", "xyz", "html", "<v/>")
            mark_raw_filing_parsed(conn, "senate", "xyz",
                                    status="parse_error",
                                    error="bad layout")
            row = conn.execute(
                "SELECT parse_status, parse_error FROM raw_filings "
                "WHERE filing_doc_id='xyz'"
            ).fetchone()
        assert row[0] == "parse_error"
        assert row[1] == "bad layout"


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

class TestQuery:
    def test_empty_returns_empty_list(self, tmp_db):
        with connect(tmp_db) as conn:
            assert query_trades(conn) == []
            assert counts_by_chamber(conn) == {}

    def test_query_by_ticker(self, tmp_db):
        with connect(tmp_db) as conn:
            # Use distinct filing_doc_id per row so UNIQUE constraint
            # doesn't treat the two AAPL inserts as duplicates
            for i, t in enumerate(("AAPL", "MSFT", "AAPL")):
                insert_trade(conn, {
                    "chamber": "house", "member_name": "X",
                    "filing_doc_id": f"doc-{i}",
                    "transaction_date": f"2025-01-0{i+1}",
                    "asset_description": t, "ticker": t,
                    "amount_range": "A", "amount_low": 1, "amount_high": 2,
                })
            rows = query_trades(conn, ticker="AAPL")
            assert len(rows) == 2
            assert all(r["ticker"] == "AAPL" for r in rows)

    def test_query_by_chamber(self, tmp_db):
        with connect(tmp_db) as conn:
            insert_trade(conn, {"chamber": "house", "member_name": "A",
                                 "filing_doc_id": "1",
                                 "asset_description": "x", "ticker": "X",
                                 "transaction_date": "2025-01-01",
                                 "amount_range": "A"})
            insert_trade(conn, {"chamber": "senate", "member_name": "B",
                                 "filing_doc_id": "2",
                                 "asset_description": "y", "ticker": "Y",
                                 "transaction_date": "2025-01-01",
                                 "amount_range": "A"})
            assert len(query_trades(conn, chamber="house")) == 1
            assert len(query_trades(conn, chamber="senate")) == 1
            assert counts_by_chamber(conn) == {"house": 1, "senate": 1}
