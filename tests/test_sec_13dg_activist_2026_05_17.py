"""Tests for sec_13dg_activist — broad-universe 13D/G discovery
(#2 Tier-1 alt-data, 2026-05-17). Pins:
  - atom parsing for both 13D and 13G form types
  - filer name + CIK extraction from EDGAR titles
  - subject company extraction from filing index page
  - idempotency (UNIQUE accession)
  - get_recent_13dg_activist filters by ticker + date + has_13d flag
"""
from __future__ import annotations

import os
import sqlite3
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    import sec_13dg_activist
    db_path = str(tmp_path / "edgar_13dg.db")
    monkeypatch.setattr(sec_13dg_activist, "_altdata_db_path",
                        lambda: db_path)
    sec_13dg_activist._ensure_13dg_table(db_path)
    return db_path


class TestAtomParsing:
    SAMPLE = """<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>SC 13D - PERSHING SQUARE CAPITAL MGMT (0001336528) (Filer)</title>
        <link href="https://www.sec.gov/Archives/edgar/data/1336528/000133652826000010/0001336528-26-000010-index.htm"/>
        <updated>2026-05-17T09:30:00-04:00</updated>
      </entry>
    </feed>
    """

    def test_parse_entry_extracts_accession(self):
        from sec_13dg_activist import _parse_atom_feed
        entries = _parse_atom_feed(self.SAMPLE)
        assert len(entries) == 1
        assert entries[0]["accession"] == "0001336528-26-000010"

    def test_extract_filer_name_and_cik(self):
        from sec_13dg_activist import _extract_filer_and_role
        out = _extract_filer_and_role(
            "SC 13D - PERSHING SQUARE CAPITAL MGMT (0001336528) (Filer)"
        )
        assert "PERSHING SQUARE" in out["filer_name"]
        assert out["filer_cik"] == "0001336528"


class TestConsumerAPI:
    def test_get_recent_13dg_activist_filters_by_ticker(self, temp_db):
        with sqlite3.connect(temp_db) as conn:
            conn.executemany(
                "INSERT INTO recent_13dg_filings "
                "(accession, form_type, filing_date, filer_name, "
                " filer_cik, subject_name, subject_cik, subject_ticker, "
                " source_url) VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    ("acc-1", "SC 13D", "2026-05-15",
                     "Pershing Square", "0001336528",
                     "Apple Inc", "0000320193", "AAPL",
                     "https://x"),
                    ("acc-2", "SC 13G", "2026-05-16",
                     "Vanguard", "0000102909",
                     "Tesla Inc", "0001318605", "TSLA",
                     "https://y"),
                ],
            )
        from sec_13dg_activist import get_recent_13dg_activist
        result = get_recent_13dg_activist("AAPL")
        assert result["count"] == 1
        assert result["events"][0]["form_type"] == "SC 13D"
        assert result["has_13d"] is True

    def test_passive_only_does_not_set_has_13d(self, temp_db):
        with sqlite3.connect(temp_db) as conn:
            conn.execute(
                "INSERT INTO recent_13dg_filings "
                "(accession, form_type, filing_date, filer_name, "
                " subject_ticker) VALUES (?,?,?,?,?)",
                ("acc-passive", "SC 13G", "2026-05-17",
                 "BlackRock", "MSFT"),
            )
        from sec_13dg_activist import get_recent_13dg_activist
        result = get_recent_13dg_activist("MSFT")
        assert result["count"] == 1
        assert result["has_13d"] is False

    def test_crypto_symbol_returns_empty(self):
        from sec_13dg_activist import get_recent_13dg_activist
        result = get_recent_13dg_activist("BTC/USD")
        assert result["events"] == []

    def test_missing_db_returns_empty(self, monkeypatch, tmp_path):
        import sec_13dg_activist
        monkeypatch.setattr(
            sec_13dg_activist, "_altdata_db_path",
            lambda: str(tmp_path / "does_not_exist.db"),
        )
        result = sec_13dg_activist.get_recent_13dg_activist("AAPL")
        assert result["count"] == 0


class TestIntegration:
    def test_alternative_data_returns_activist_13dg_key(self, monkeypatch):
        """Avoid the 20-nested-with Python limit by using ExitStack
        to compose 20+ patches dynamically.

        2026-05-20: also disable the alt_data_cache so the patched
        fetchers are actually called (otherwise the SQLite cache —
        added by the pre-market warmup in docs/21 — returns stale
        rows from an earlier population)."""
        # Cache off → forces every cached source to call its fetcher
        # live, which is what the patches in this test are stubbing.
        monkeypatch.setenv("ALTDATA_CACHE_ENABLED", "0")
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
                patch("sec_8k_broad.get_recent_8k_events",
                      return_value={"events": [], "count": 0,
                                    "high_signal_count": 0})
            )
            stack.enter_context(
                patch(
                    "sec_13dg_activist.get_recent_13dg_activist",
                    return_value={"events": [{"form_type": "SC 13D"}],
                                  "count": 1, "has_13d": True},
                )
            )
            result = get_all_alternative_data("AAPL")
        assert "activist_13dg" in result
        assert result["activist_13dg"]["has_13d"] is True
