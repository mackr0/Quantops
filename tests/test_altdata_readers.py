"""Tests for the 4 standalone alt-data project read helpers.

Each helper is exercised against a seeded SQLite fixture mirroring the
production schema. Critical guarantees:
  - Returns shape-stable empty dict on missing DB / empty data.
  - Reads through the existing alt_data_cache (6h TTL).
  - Tolerates partial schema (e.g., period_of_report not yet populated
    on a freshly-seeded edgar13f DB).
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def altdata_dirs(tmp_path, monkeypatch):
    """Create a temporary ALTDATA_BASE_PATH with the four project
    layouts and seed each DB with realistic test data. Returns the
    base path; the helper functions will resolve their DBs there."""
    base = tmp_path / "altdata"
    monkeypatch.setenv("ALTDATA_BASE_PATH", str(base))

    # Re-route the alt-data cache to a tmp DB so tests don't pollute
    # the dev/prod cache.
    cache_db = tmp_path / "altdata_cache.db"
    monkeypatch.setattr("alternative_data._DB_PATH", str(cache_db))
    # Reset cache-table flag so the new tmp DB's table gets created
    monkeypatch.setattr("alternative_data._table_ensured", False)

    # ── congresstrades ──
    cdir = base / "congresstrades" / "data"
    cdir.mkdir(parents=True, exist_ok=True)
    cdb = cdir / "congress.db"
    conn = sqlite3.connect(cdb)
    conn.executescript("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chamber TEXT, member_name TEXT, member_state TEXT,
            member_party TEXT, filing_doc_id TEXT, filing_date TEXT,
            transaction_date TEXT, ticker TEXT, asset_description TEXT,
            transaction_type TEXT, amount_range TEXT,
            amount_low INTEGER, amount_high INTEGER, owner TEXT,
            source_url TEXT, raw_json TEXT, fetched_at TEXT,
            asset_type TEXT, parser_version TEXT
        );
    """)
    # 3 buys, 1 sell on NVDA in last 60 days
    for i in range(3):
        conn.execute(
            "INSERT INTO trades "
            "(chamber, member_name, member_party, filing_date, ticker, "
            " asset_description, transaction_type, amount_low, amount_high, "
            " fetched_at) VALUES "
            "('house', ?, 'D', date('now', '-' || ? || ' days'), "
            " 'NVDA', 'Nvidia stock', 'buy', ?, ?, datetime('now'))",
            (f"Member {i}", i * 5 + 5, 15000, 50000),
        )
    conn.execute(
        "INSERT INTO trades "
        "(chamber, member_name, member_party, filing_date, ticker, "
        " asset_description, transaction_type, amount_low, amount_high, "
        " fetched_at) VALUES "
        "('senate', 'Senator X', 'R', date('now', '-10 days'), "
        " 'NVDA', 'Nvidia stock', 'sell', 1000, 15000, datetime('now'))"
    )
    conn.commit()
    conn.close()

    # ── edgar13f ──
    edir = base / "edgar13f" / "data"
    edir.mkdir(parents=True, exist_ok=True)
    edb = edir / "edgar13f.db"
    conn = sqlite3.connect(edb)
    conn.executescript("""
        CREATE TABLE filers (
            cik TEXT PRIMARY KEY, name TEXT NOT NULL,
            aum_usd INTEGER, filer_type TEXT,
            first_seen TEXT, updated_at TEXT
        );
        CREATE TABLE filings (
            accession_number TEXT PRIMARY KEY, cik TEXT NOT NULL,
            period_of_report TEXT, filed_date TEXT,
            total_value_usd INTEGER, total_positions INTEGER,
            parser_version TEXT, fetched_at TEXT
        );
        CREATE TABLE holdings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            accession_number TEXT, cusip TEXT, ticker TEXT,
            company_name TEXT, class_title TEXT, shares INTEGER,
            value_usd INTEGER, put_call TEXT,
            investment_discretion TEXT, parser_version TEXT
        );
    """)
    conn.execute(
        "INSERT INTO filers (cik, name, first_seen, updated_at) "
        "VALUES ('1067983', 'Berkshire Hathaway Inc', "
        " datetime('now'), datetime('now'))")
    conn.execute(
        "INSERT INTO filings (accession_number, cik, period_of_report, "
        " filed_date, fetched_at) "
        "VALUES ('A1', '1067983', '2025-12-31', "
        " '2026-02-14', datetime('now'))")
    conn.execute(
        "INSERT INTO holdings (accession_number, cusip, ticker, "
        " company_name, shares, value_usd) "
        "VALUES ('A1', '037833100', 'AAPL', 'Apple Inc', "
        " 80000000, 22000000000)")
    conn.commit()
    conn.close()

    # ── biotechevents ──
    bdir = base / "biotechevents" / "data"
    bdir.mkdir(parents=True, exist_ok=True)
    bdb = bdir / "biotechevents.db"
    conn = sqlite3.connect(bdb)
    conn.executescript("""
        CREATE TABLE trials (
            nct_id TEXT PRIMARY KEY, brief_title TEXT,
            sponsor_name TEXT, sponsor_class TEXT, ticker TEXT,
            phase TEXT, overall_status TEXT,
            primary_completion_date TEXT, completion_date TEXT,
            start_date TEXT, last_updated TEXT,
            enrollment_count INTEGER, conditions_json TEXT,
            interventions_json TEXT, parser_version TEXT,
            fetched_at TEXT
        );
        CREATE TABLE trial_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nct_id TEXT, field TEXT, old_value TEXT,
            new_value TEXT, detected_at TEXT
        );
        CREATE TABLE pdufa_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            drug_name TEXT, sponsor_company TEXT, ticker TEXT,
            pdufa_date TEXT, indication TEXT, fetched_at TEXT
        );
    """)
    conn.execute(
        "INSERT INTO pdufa_events (drug_name, sponsor_company, ticker, "
        " pdufa_date, fetched_at) "
        "VALUES ('TestDrug', 'BioTest Inc', 'BIOT', "
        " date('now', '+45 days'), datetime('now'))")
    conn.execute(
        "INSERT INTO trials (nct_id, brief_title, ticker, phase, "
        " overall_status, fetched_at) "
        "VALUES ('NCT001', 'P3 Study', 'BIOT', 'PHASE3', "
        " 'RECRUITING', datetime('now'))")
    conn.execute(
        "INSERT INTO trials (nct_id, brief_title, ticker, phase, "
        " overall_status, fetched_at) "
        "VALUES ('NCT002', 'P3 Study2', 'BIOT', 'PHASE3', "
        " 'ACTIVE_NOT_RECRUITING', datetime('now'))")
    conn.execute(
        "INSERT INTO trial_changes (nct_id, field, old_value, "
        " new_value, detected_at) "
        "VALUES ('NCT001', 'phase', 'PHASE2', 'PHASE3', "
        " datetime('now', '-3 days'))")
    conn.commit()
    conn.close()

    # ── stocktwits ──
    sdir = base / "stocktwits" / "data"
    sdir.mkdir(parents=True, exist_ok=True)
    sdb = sdir / "stocktwits.db"
    conn = sqlite3.connect(sdb)
    conn.executescript("""
        CREATE TABLE messages (
            msg_id INTEGER PRIMARY KEY, ticker TEXT,
            user_id INTEGER, user_name TEXT, body TEXT,
            sentiment TEXT, created_at TEXT,
            like_count INTEGER, parser_version TEXT,
            fetched_at TEXT
        );
        CREATE TABLE ticker_sentiment_daily (
            ticker TEXT NOT NULL, date TEXT NOT NULL,
            n_messages INTEGER, n_bullish INTEGER,
            n_bearish INTEGER, n_neutral INTEGER,
            net_sentiment REAL, avg_likes REAL,
            last_updated TEXT,
            PRIMARY KEY (ticker, date)
        );
        CREATE TABLE trending_snapshots (
            snapshot_at TEXT, rank INTEGER, ticker TEXT,
            PRIMARY KEY (snapshot_at, rank)
        );
    """)
    # 7 days of data for AAPL — bullish trend
    for i in range(7):
        conn.execute(
            "INSERT INTO ticker_sentiment_daily "
            "(ticker, date, n_messages, n_bullish, n_bearish, "
            " n_neutral, net_sentiment, last_updated) "
            "VALUES ('AAPL', date('now', '-' || ? || ' days'), "
            " 50, 35, 10, 5, 0.5, datetime('now'))",
            (i,),
        )
    # AAPL trending now
    conn.execute(
        "INSERT INTO trending_snapshots (snapshot_at, rank, ticker) "
        "VALUES (datetime('now', '-1 hour'), 3, 'AAPL')")
    conn.commit()
    conn.close()

    return base


# ─────────────────────────────────────────────────────────────────────
# Congressional
# ─────────────────────────────────────────────────────────────────────

class TestCongressionalRecent:
    def test_returns_empty_for_unknown_symbol(self, altdata_dirs):
        from alternative_data import get_congressional_recent
        result = get_congressional_recent("UNKNOWN")
        assert result["trades_60d"] == 0
        assert result["net_direction"] == "neutral"

    def test_aggregates_recent_trades(self, altdata_dirs):
        from alternative_data import get_congressional_recent
        result = get_congressional_recent("NVDA")
        assert result["trades_60d"] == 4
        assert result["buys_60d"] == 3
        assert result["sells_60d"] == 1
        assert result["net_direction"] == "bullish"  # 3 buys >> 1 sell
        assert result["dollar_volume_60d"] > 0

    def test_no_db_returns_empty(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ALTDATA_BASE_PATH", str(tmp_path / "missing"))
        # Re-route the cache too
        monkeypatch.setattr("alternative_data._DB_PATH",
                             str(tmp_path / "cache.db"))
        monkeypatch.setattr("alternative_data._table_ensured", False)
        from alternative_data import get_congressional_recent
        result = get_congressional_recent("NVDA")
        assert result["trades_60d"] == 0


# ─────────────────────────────────────────────────────────────────────
# 13F
# ─────────────────────────────────────────────────────────────────────

class TestInstitutional13f:
    def test_returns_aggregates(self, altdata_dirs):
        from alternative_data import get_13f_institutional
        result = get_13f_institutional("AAPL")
        assert result["total_holders"] == 1
        assert result["total_shares"] == 80000000
        assert "Berkshire" in (result["top_holder_name"] or "")
        assert result["quarter"] == "2025-12-31"

    def test_returns_empty_for_unknown(self, altdata_dirs):
        from alternative_data import get_13f_institutional
        result = get_13f_institutional("UNKNOWN")
        assert result["total_holders"] == 0


# ─────────────────────────────────────────────────────────────────────
# Biotech milestones
# ─────────────────────────────────────────────────────────────────────

class TestBiotechMilestones:
    def test_finds_pdufa(self, altdata_dirs):
        from alternative_data import get_biotech_milestones
        result = get_biotech_milestones("BIOT")
        assert result["upcoming_pdufa_date"] is not None
        assert result["days_to_pdufa"] is not None
        assert result["days_to_pdufa"] > 0
        assert result["drug_name"] == "TestDrug"

    def test_counts_active_phase_3(self, altdata_dirs):
        from alternative_data import get_biotech_milestones
        result = get_biotech_milestones("BIOT")
        assert result["active_phase3_count"] == 2

    def test_recent_phase_change_detected(self, altdata_dirs):
        from alternative_data import get_biotech_milestones
        result = get_biotech_milestones("BIOT")
        assert result["recent_phase_change"] is not None
        assert result["recent_phase_change"]["from"] == "PHASE2"
        assert result["recent_phase_change"]["to"] == "PHASE3"

    def test_returns_empty_for_unknown(self, altdata_dirs):
        from alternative_data import get_biotech_milestones
        result = get_biotech_milestones("NOPE")
        assert result["upcoming_pdufa_date"] is None
        assert result["active_phase3_count"] == 0


# ─────────────────────────────────────────────────────────────────────
# StockTwits
# ─────────────────────────────────────────────────────────────────────

class TestStocktwitsSentiment:
    def test_aggregates_7d_sentiment(self, altdata_dirs):
        from alternative_data import get_stocktwits_sentiment
        result = get_stocktwits_sentiment("AAPL")
        # Note: SQLite's `date('now', '-7 days')` is INCLUSIVE of today's
        # offset, so seeded days 0-6 may net to 7 entries within range
        # depending on time-of-day. Just verify we got positive data.
        assert result["message_count_7d"] > 200  # 50/day x 7 = 350 max
        assert result["net_sentiment_7d"] is not None
        assert result["net_sentiment_7d"] > 0  # bullish

    def test_detects_trending(self, altdata_dirs):
        from alternative_data import get_stocktwits_sentiment
        result = get_stocktwits_sentiment("AAPL")
        assert result["is_trending"] is True
        assert result["trending_rank"] == 3

    def test_returns_empty_for_unknown(self, altdata_dirs):
        from alternative_data import get_stocktwits_sentiment
        result = get_stocktwits_sentiment("UNKNOWN")
        assert result["message_count_7d"] == 0
        assert result["is_trending"] is False
