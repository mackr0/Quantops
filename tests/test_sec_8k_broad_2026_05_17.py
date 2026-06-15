"""Tests for sec_8k_broad — broad-universe 8-K discovery (#1 Tier-1,
2026-05-17 alt-data expansion).

Pin the contract:
  - atom feed parsing (no network)
  - item-code extraction from filing text
  - idempotency (re-running doesn't double-insert)
  - get_recent_8k_events filters by ticker + date window
  - high-signal item tagging (1.01 → material_agreement, etc.)
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point _altdata_db_path at a temp file so tests don't touch
    the real altdata DB."""
    import sec_8k_broad
    db_path = str(tmp_path / "edgar_8k.db")
    monkeypatch.setattr(sec_8k_broad, "_altdata_db_path",
                        lambda: db_path)
    sec_8k_broad._ensure_8k_table(db_path)
    return db_path


# ─────────────────────────────────────────────────────────────────────
# Atom feed parsing
# ─────────────────────────────────────────────────────────────────────

class TestAtomFeedParsing:
    SAMPLE_ATOM = """<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>8-K - APPLE INC (0000320193) (Filer)</title>
        <link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/320193/000032019326000001/0000320193-26-000001-index.htm"/>
        <updated>2026-05-17T09:30:00-04:00</updated>
      </entry>
      <entry>
        <title>8-K - TESLA INC (0001318605) (Filer)</title>
        <link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/1318605/000131860526000050/0001318605-26-000050-index.htm"/>
        <updated>2026-05-17T10:15:00-04:00</updated>
      </entry>
    </feed>
    """

    def test_parse_atom_extracts_each_entry(self):
        from sec_8k_broad import _parse_atom_feed
        entries = _parse_atom_feed(self.SAMPLE_ATOM)
        assert len(entries) == 2
        assert entries[0]["accession"] == "0000320193-26-000001"
        assert "APPLE INC" in entries[0]["title"]
        assert entries[1]["accession"] == "0001318605-26-000050"

    def test_company_and_cik_extraction(self):
        from sec_8k_broad import _extract_company_and_cik
        out = _extract_company_and_cik(
            "8-K - APPLE INC (0000320193) (Filer)"
        )
        assert out["company_name"] == "APPLE INC"
        assert out["cik"] == "0000320193"

    def test_company_with_special_chars(self):
        from sec_8k_broad import _extract_company_and_cik
        out = _extract_company_and_cik(
            "8-K - BERKSHIRE HATHAWAY INC /DE/ (0001067983) (Filer)"
        )
        assert "BERKSHIRE HATHAWAY" in out["company_name"]
        assert out["cik"] == "0001067983"


# ─────────────────────────────────────────────────────────────────────
# Item extraction
# ─────────────────────────────────────────────────────────────────────

class TestItemExtraction:
    def test_single_item_extracted(self):
        from sec_8k_broad import _ITEM_RE
        text = "Item 5.02 Departure of Directors or Certain Officers..."
        matches = _ITEM_RE.findall(text)
        assert matches == ["5.02"]

    def test_multiple_items_in_one_filing(self):
        """A typical 8-K has multiple items: e.g. earnings announcement
        (2.02) + accompanying press release exhibit (9.01)."""
        from sec_8k_broad import _ITEM_RE
        text = (
            "Item 2.02 Results of Operations and Financial Condition\n"
            "blah blah\n"
            "Item 9.01 Financial Statements and Exhibits\n"
        )
        matches = _ITEM_RE.findall(text)
        assert "2.02" in matches and "9.01" in matches

    def test_case_insensitive(self):
        from sec_8k_broad import _ITEM_RE
        text = "ITEM 1.01 Entry into a Material Definitive Agreement"
        matches = _ITEM_RE.findall(text)
        assert matches == ["1.01"]


# ─────────────────────────────────────────────────────────────────────
# Database + idempotency
# ─────────────────────────────────────────────────────────────────────

class TestPersistence:
    def test_schema_created(self, temp_db):
        with sqlite3.connect(temp_db) as conn:
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        assert "recent_8k_filings" in tables
        assert "scrape_runs" in tables

    def test_re_scrape_is_idempotent(self, temp_db):
        """Re-running scrape with same atom feed doesn't double-insert."""
        from sec_8k_broad import scrape_recent_8k_filings
        sample = TestAtomFeedParsing.SAMPLE_ATOM
        # Mock the rate-limited fetch + filing-text extraction
        with patch(
            "sec_filings._rate_limited_get",
            return_value=sample.encode("utf-8"),
        ), patch(
            "sec_8k_broad._extract_items_from_filing",
            return_value=["2.02"],
        ), patch(
            "sec_8k_broad._build_reverse_cik_map",
            return_value={"0000320193": "AAPL", "0001318605": "TSLA"},
        ):
            r1 = scrape_recent_8k_filings()
            r2 = scrape_recent_8k_filings()
        assert r1["new"] == 2
        assert r2["new"] == 0  # same accessions, ON CONFLICT IGNORE
        # Total rows in DB = 2 (not 4)
        with sqlite3.connect(temp_db) as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM recent_8k_filings"
            ).fetchone()[0]
        assert n == 2


# ─────────────────────────────────────────────────────────────────────
# Consumer API
# ─────────────────────────────────────────────────────────────────────

class TestGetRecentEvents:
    def test_returns_only_target_symbol(self, temp_db):
        """get_recent_8k_events(symbol) filters by ticker."""
        # 2026-06-15 — RELATIVE dates. These were hardcoded
        # 2026-05-15/16 at authoring; once "today" passed
        # authoring+30d the filings aged out of the default 30-day
        # window and this asserted count==0 (caught live 2026-06-15,
        # exactly 31 days after the 05-15 filing). Mirror
        # test_date_window_filter's relative-date pattern so the
        # fixture never drifts out of the window again.
        from datetime import datetime, timedelta, timezone
        recent = (datetime.now(tz=timezone.utc)
                  - timedelta(days=2)).date().isoformat()
        recent2 = (datetime.now(tz=timezone.utc)
                   - timedelta(days=1)).date().isoformat()
        with sqlite3.connect(temp_db) as conn:
            conn.executemany(
                "INSERT INTO recent_8k_filings "
                "(accession, filing_date, company_name, cik, ticker, "
                " items_json, source_url) VALUES (?,?,?,?,?,?,?)",
                [
                    ("acc-1", recent, "Apple Inc",
                     "0000320193", "AAPL", "2.02,9.01",
                     "https://x"),
                    ("acc-2", recent2, "Tesla Inc",
                     "0001318605", "TSLA", "1.01",
                     "https://y"),
                ],
            )
        from sec_8k_broad import get_recent_8k_events
        result = get_recent_8k_events("AAPL")
        assert result["count"] == 1
        assert result["events"][0]["company_name"] == "Apple Inc"

    def test_high_signal_tagging(self, temp_db):
        """Items 1.01/2.02/5.02/4.02 etc. get tagged with semantic
        names so the AI prompt can summarize."""
        # 2026-06-15 — relative date (see test_returns_only_target_
        # symbol): hardcoded 2026-05-17 would age out of the 30-day
        # window and silently start returning 0 events.
        from datetime import datetime, timedelta, timezone
        recent = (datetime.now(tz=timezone.utc)
                  - timedelta(days=2)).date().isoformat()
        with sqlite3.connect(temp_db) as conn:
            conn.execute(
                "INSERT INTO recent_8k_filings "
                "(accession, filing_date, ticker, items_json, source_url) "
                "VALUES (?,?,?,?,?)",
                ("acc-test", recent, "MSFT", "1.01,5.02",
                 "https://z"),
            )
        from sec_8k_broad import get_recent_8k_events
        result = get_recent_8k_events("MSFT")
        tags = result["events"][0]["item_tags"]
        assert "material_agreement" in tags
        assert "officer_change" in tags
        assert result["high_signal_count"] == 1

    def test_date_window_filter(self, temp_db):
        """Events older than `days` are excluded."""
        from datetime import datetime, timedelta, timezone
        old_date = (datetime.now(tz=timezone.utc)
                    - timedelta(days=60)).date().isoformat()
        with sqlite3.connect(temp_db) as conn:
            conn.execute(
                "INSERT INTO recent_8k_filings "
                "(accession, filing_date, ticker, items_json) "
                "VALUES (?,?,?,?)",
                ("acc-old", old_date, "AAPL", "1.01"),
            )
        from sec_8k_broad import get_recent_8k_events
        # default days=30 → 60-day-old event excluded
        assert get_recent_8k_events("AAPL", days=30)["count"] == 0
        # days=90 → 60-day-old event included
        assert get_recent_8k_events("AAPL", days=90)["count"] == 1

    def test_crypto_symbol_returns_empty(self, temp_db):
        """Crypto pairs ('/' in symbol) don't have 8-Ks."""
        from sec_8k_broad import get_recent_8k_events
        result = get_recent_8k_events("BTC/USD")
        assert result == {"events": [], "count": 0, "high_signal_count": 0}

    def test_missing_db_returns_empty_not_crash(self, monkeypatch, tmp_path):
        """If the DB file doesn't exist yet (fresh deploy before
        first scrape), the consumer returns empty rather than
        crashing."""
        import sec_8k_broad
        monkeypatch.setattr(
            sec_8k_broad, "_altdata_db_path",
            lambda: str(tmp_path / "does_not_exist.db"),
        )
        result = sec_8k_broad.get_recent_8k_events("AAPL")
        assert result["count"] == 0


# ─────────────────────────────────────────────────────────────────────
# Integration with alternative_data
# ─────────────────────────────────────────────────────────────────────

class TestAlternativeDataIntegration:
    def test_get_all_alternative_data_includes_recent_8k_events_key(self):
        """The 'recent_8k_events' key is part of the canonical alt-
        data return — adding 8-K to the inventory means the AI
        prompt automatically sees it.

        Uses ExitStack to compose 20+ patches (avoids Python's
        20-nested-with statement compiler limit)."""
        from contextlib import ExitStack
        from alternative_data import get_all_alternative_data
        per_symbol_stubs = [
            "get_insider_activity", "get_short_interest", "get_fundamentals",
            "get_options_unusual", "get_intraday_patterns",
            "get_finra_short_volume", "get_insider_cluster",
            "get_analyst_estimates", "get_insider_earnings_signal",
            "get_dark_pool_volume", "get_earnings_surprise",
            "get_congressional_recent", "get_13f_institutional",
            "get_biotech_milestones", "get_stocktwits_sentiment",
            "get_google_trends_signal", "get_wikipedia_pageviews_signal",
            "get_app_store_ranking",
        ]
        with ExitStack() as stack:
            for name in per_symbol_stubs:
                stack.enter_context(
                    patch(f"alternative_data.{name}", return_value={})
                )
            stack.enter_context(
                patch("macro_data.get_all_macro_data", return_value={})
            )
            stack.enter_context(
                patch(
                    "sec_8k_broad.get_recent_8k_events",
                    return_value={"events": [{"date": "2026-05-17"}],
                                  "count": 1, "high_signal_count": 1},
                )
            )
            stack.enter_context(
                patch(
                    "sec_13dg_activist.get_recent_13dg_activist",
                    return_value={"events": [], "count": 0,
                                  "has_13d": False},
                )
            )
            result = get_all_alternative_data("AAPL")
        assert "recent_8k_events" in result
        assert result["recent_8k_events"]["count"] == 1
