"""OPEN_ITEMS #6 — PDUFA scraper tests."""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


SAMPLE_HTML = """
<html><body>
<table>
<tr><th>Date</th><th>Type</th><th>Ticker</th><th>Drug</th></tr>
<tr><td>2026-08-15</td><td>PDUFA</td><td>BMY</td><td>Eliquis Pediatric</td></tr>
<tr><td>Sep 22, 2026</td><td>PDUFA</td><td>MRNA</td><td>mRNA-1647</td></tr>
<tr><td>10/01/2026</td><td>AdComm</td><td>PFE</td><td>Some other drug</td></tr>
<tr><td>2026-12-01</td><td>PDUFA</td><td>GILD</td><td>Lenacapavir Long-Acting</td></tr>
</table>
</body></html>
"""


class TestParseBiopharmCatalyst:
    def test_extracts_pdufa_rows_only(self):
        from pdufa_scraper import parse_biopharmcatalyst
        rows = parse_biopharmcatalyst(SAMPLE_HTML)
        # 3 PDUFA rows; AdComm row excluded
        tickers = {r["ticker"] for r in rows}
        assert "BMY" in tickers
        assert "MRNA" in tickers
        assert "GILD" in tickers
        assert "PFE" not in tickers

    def test_parses_iso_date(self):
        from pdufa_scraper import parse_biopharmcatalyst
        rows = parse_biopharmcatalyst(SAMPLE_HTML)
        bmy = next(r for r in rows if r["ticker"] == "BMY")
        assert bmy["pdufa_date"] == "2026-08-15"

    def test_parses_long_form_date(self):
        from pdufa_scraper import parse_biopharmcatalyst
        rows = parse_biopharmcatalyst(SAMPLE_HTML)
        mrna = next(r for r in rows if r["ticker"] == "MRNA")
        assert mrna["pdufa_date"] == "2026-09-22"

    def test_extracts_drug_name(self):
        from pdufa_scraper import parse_biopharmcatalyst
        rows = parse_biopharmcatalyst(SAMPLE_HTML)
        bmy = next(r for r in rows if r["ticker"] == "BMY")
        assert "Eliquis" in bmy["drug_name"]

    def test_empty_html_returns_empty(self):
        from pdufa_scraper import parse_biopharmcatalyst
        assert parse_biopharmcatalyst("") == []
        assert parse_biopharmcatalyst("<html></html>") == []

    def test_dedupes_identical_rows(self):
        """Same (ticker, drug, date) appearing twice in the page only
        produces one row."""
        from pdufa_scraper import parse_biopharmcatalyst
        dup = SAMPLE_HTML + SAMPLE_HTML
        rows = parse_biopharmcatalyst(dup)
        # Each ticker should appear at most once
        tickers = [r["ticker"] for r in rows]
        assert len(tickers) == len(set(tickers))


class TestSyncToAltdataDb:
    def test_creates_table_and_writes(self, tmp_path):
        from pdufa_scraper import sync_pdufa_events_to_altdata_db
        db = str(tmp_path / "biotechevents.db")
        events = [
            {"ticker": "BMY", "drug_name": "Eliquis",
             "pdufa_date": "2026-08-15", "source": "test"},
            {"ticker": "MRNA", "drug_name": "mRNA-1647",
             "pdufa_date": "2026-09-22", "source": "test"},
        ]
        n = sync_pdufa_events_to_altdata_db(events, db_path=db)
        assert n == 2
        conn = sqlite3.connect(db)
        rows = conn.execute("SELECT ticker, pdufa_date FROM pdufa_events").fetchall()
        conn.close()
        assert len(rows) == 2
        assert ("BMY", "2026-08-15") in rows

    def test_upsert_replaces_duplicate(self, tmp_path):
        from pdufa_scraper import sync_pdufa_events_to_altdata_db
        db = str(tmp_path / "biotechevents.db")
        e = {"ticker": "BMY", "drug_name": "Eliquis",
              "pdufa_date": "2026-08-15", "source": "v1"}
        sync_pdufa_events_to_altdata_db([e], db_path=db)
        # Re-sync with updated source — should not duplicate
        e["source"] = "v2"
        sync_pdufa_events_to_altdata_db([e], db_path=db)
        conn = sqlite3.connect(db)
        n = conn.execute("SELECT COUNT(*) FROM pdufa_events").fetchone()[0]
        sources = conn.execute("SELECT source FROM pdufa_events").fetchall()
        conn.close()
        assert n == 1
        assert sources[0][0] == "v2"

    def test_empty_events_writes_nothing(self, tmp_path):
        from pdufa_scraper import sync_pdufa_events_to_altdata_db
        db = str(tmp_path / "biotechevents.db")
        n = sync_pdufa_events_to_altdata_db([], db_path=db)
        assert n == 0


class TestFetchAndRunSync:
    def test_fetch_uses_fallback_on_http_failure(self):
        from pdufa_scraper import fetch_pdufa_events
        with patch("pdufa_scraper._fetch_html", return_value=None):
            events = fetch_pdufa_events()
        # Falls back to PDUFA_FALLBACK_SEED (empty by default — no
        # crash, just zero rows).
        assert events == []

    def test_run_full_sync_returns_counts(self, tmp_path, monkeypatch):
        from pdufa_scraper import run_full_sync
        db = str(tmp_path / "biotechevents.db")
        monkeypatch.setattr(
            "pdufa_scraper._altdata_db_path", lambda: db,
        )
        with patch("pdufa_scraper._fetch_html", return_value=SAMPLE_HTML):
            n_fetched, n_written = run_full_sync()
        assert n_fetched >= 3
        assert n_written >= 3


class TestDateParsing:
    def test_iso_format(self):
        from pdufa_scraper import _parse_iso_date
        assert _parse_iso_date("2026-08-15") == "2026-08-15"

    def test_us_format(self):
        from pdufa_scraper import _parse_iso_date
        assert _parse_iso_date("8/15/2026") == "2026-08-15"

    def test_long_form(self):
        from pdufa_scraper import _parse_iso_date
        assert _parse_iso_date("Aug 15, 2026") == "2026-08-15"
        assert _parse_iso_date("September 1, 2026") == "2026-09-01"

    def test_invalid(self):
        from pdufa_scraper import _parse_iso_date
        assert _parse_iso_date("Q1 2026") is None
        assert _parse_iso_date("") is None
        assert _parse_iso_date("not a date") is None
