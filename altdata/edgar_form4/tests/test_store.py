"""Tests for edgar_form4.store — schema + CRUD + aggregate reader."""
from __future__ import annotations

import os
import sqlite3
import tempfile
from contextlib import closing

import pytest

from edgar_form4.store import (
    connect,
    counts_by_date,
    get_recent_insider_activity,
    init_db,
    insert_filing,
    insert_txn,
    upsert_company,
)


@pytest.fixture
def tmp_db(tmp_path):
    db = str(tmp_path / "form4.db")
    init_db(db)
    return db


class TestSchema:
    def test_init_db_idempotent(self, tmp_path):
        """Calling init_db twice on the same path is a no-op."""
        db = str(tmp_path / "f.db")
        init_db(db)
        init_db(db)  # must not raise
        with closing(sqlite3.connect(db)) as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        for t in ("companies", "form4_filings", "insider_txns",
                  "raw_filings", "scrape_runs"):
            assert t in tables


class TestCompanies:
    def test_upsert_and_lookup(self, tmp_db):
        with closing(sqlite3.connect(tmp_db)) as conn:
            upsert_company(conn, cik="0000320193", ticker="AAPL",
                            name="Apple Inc")
            conn.commit()
        with connect(tmp_db) as conn:
            from edgar_form4.store import cik_for_ticker
            assert cik_for_ticker(conn, "AAPL") == "0000320193"
            assert cik_for_ticker(conn, "aapl") == "0000320193"  # case-insens
            assert cik_for_ticker(conn, "BOGUS") is None


class TestPreferTicker:
    """Regression test for the JPM bug (2026-05-16). SEC's
    `company_tickers.json` has multiple entries per CIK — one per
    share class (JPM common + JPM-PM preferred + JPM-PC + JPM-PD).
    The naive upsert kept the LAST one processed, ending up with
    'JPM-PM' as the cleanly-mapped ticker for JPMorgan Chase. The
    `_prefer_ticker` heuristic picks the common-share variant."""

    def test_no_hyphen_beats_hyphen(self):
        from edgar_form4.store import _prefer_ticker
        assert _prefer_ticker("JPM-PM", "JPM") == "JPM"
        assert _prefer_ticker("JPM", "JPM-PM") == "JPM"

    def test_shorter_beats_longer_when_both_clean(self):
        from edgar_form4.store import _prefer_ticker
        assert _prefer_ticker("AAPL", "AAPLPR") == "AAPL"

    def test_alphabetical_tie_break(self):
        from edgar_form4.store import _prefer_ticker
        # Same length, no hyphen — deterministic tie-break.
        assert _prefer_ticker("ZZZA", "AAAB") == "AAAB"

    def test_jpm_full_cycle(self, tmp_db):
        """Full path: upsert JPM common, then JPM-PM preferred — the
        canonical ticker for the CIK must be JPM (the common share)."""
        with connect(tmp_db) as conn:
            upsert_company(conn, cik="0000019617",
                            ticker="JPM", name="JPMORGAN CHASE & CO")
            upsert_company(conn, cik="0000019617",
                            ticker="JPM-PM", name="JPMORGAN CHASE & CO")
            upsert_company(conn, cik="0000019617",
                            ticker="JPM-PC", name="JPMORGAN CHASE & CO")
            row = conn.execute(
                "SELECT ticker FROM companies WHERE cik = '0000019617'"
            ).fetchone()
        assert row[0] == "JPM"

    def test_jpm_full_cycle_reverse_order(self, tmp_db):
        """Same as above but preferred shares processed FIRST.
        Common still wins."""
        with connect(tmp_db) as conn:
            upsert_company(conn, cik="0000019617",
                            ticker="JPM-PD", name="JPMORGAN CHASE & CO")
            upsert_company(conn, cik="0000019617",
                            ticker="JPM-PM", name="JPMORGAN CHASE & CO")
            upsert_company(conn, cik="0000019617",
                            ticker="JPM", name="JPMORGAN CHASE & CO")
            row = conn.execute(
                "SELECT ticker FROM companies WHERE cik = '0000019617'"
            ).fetchone()
        assert row[0] == "JPM"


class TestXmlUrlPrefix:
    """Regression test for the xslF345X06 URL prefix bug (2026-05-16).
    SEC's submissions JSON sometimes returns the XSL-renderer path
    (e.g. 'xslF345X06/doc4.xml') instead of the structured XML path
    ('doc4.xml'). The build_xml_url must strip the xsl* prefix."""

    def test_strips_xsl_prefix(self):
        from edgar_form4.scrape import _build_xml_url
        url = _build_xml_url("0000019617", "0001225208-26-005411",
                              "xslF345X06/doc4.xml")
        assert "xslF345X06" not in url
        assert url.endswith("/doc4.xml")

    def test_passthrough_without_prefix(self):
        from edgar_form4.scrape import _build_xml_url
        url = _build_xml_url("0000019617", "0001225208-26-005411",
                              "doc4.xml")
        assert url.endswith("/doc4.xml")
        # Make sure we didn't munge the CIK/accession.
        assert "19617" in url
        assert "000122520826005411" in url


class TestInsertDedup:
    def test_filing_dedup_by_accession(self, tmp_db):
        with connect(tmp_db) as conn:
            upsert_company(conn, cik="0000320193", ticker="AAPL",
                            name="Apple Inc")
            ok1 = insert_filing(conn, "0000320193-26-000001",
                                 "0000320193", "2026-05-15")
            ok2 = insert_filing(conn, "0000320193-26-000001",
                                 "0000320193", "2026-05-15")
        assert ok1 is True
        assert ok2 is False  # duplicate accession → silently dedup'd

    def test_txn_dedup_via_composite(self, tmp_db):
        with connect(tmp_db) as conn:
            upsert_company(conn, cik="0000320193", ticker="AAPL",
                            name="Apple")
            insert_filing(conn, "0000320193-26-000001",
                           "0000320193", "2026-05-15")
            kw = dict(
                conn=conn,
                accession_number="0000320193-26-000001",
                cik="0000320193",
                rpt_owner_name="COOK TIMOTHY D",
                transaction_date="2026-05-15",
                txn_code="P",
                shares=10000.0,
            )
            ok1 = insert_txn(**kw)
            ok2 = insert_txn(**kw)  # exact duplicate
        assert ok1 is True
        assert ok2 is False


class TestAggregateReader:
    def _seed_transactions(self, db, ticker, cik, txns):
        """Helper: seed multiple txns for a ticker."""
        with connect(db) as conn:
            upsert_company(conn, cik=cik, ticker=ticker, name=f"{ticker} Co")
            insert_filing(conn, accession_number=f"{cik}-26-001",
                           cik=cik, filed_date="2026-05-15")
            for i, t in enumerate(txns):
                insert_txn(
                    conn,
                    accession_number=f"{cik}-26-001",
                    cik=cik,
                    rpt_owner_name=t.get("owner", f"INSIDER_{i}"),
                    transaction_date=t["date"],
                    txn_code=t["code"],
                    shares=t.get("shares", 100.0),
                    price_per_share=t.get("price", 10.0),
                    value_usd=t.get("shares", 100.0) * t.get("price", 10.0),
                    is_officer=t.get("officer", False),
                    officer_title=t.get("title"),
                )

    def test_no_txns_returns_neutral(self, tmp_db):
        with connect(tmp_db) as conn:
            data = get_recent_insider_activity(conn, "AAPL")
        assert data["recent_buys"] == 0
        assert data["recent_sells"] == 0
        assert data["net_direction"] == "neutral"

    def test_buys_outweighing_sells_marks_buying(self, tmp_db):
        from datetime import date, timedelta
        today = date.today().isoformat()
        self._seed_transactions(tmp_db, "AAPL", "0000320193", [
            {"date": today, "code": "P", "shares": 5000, "price": 100},
            {"date": today, "code": "P", "shares": 3000, "price": 100},
            {"date": today, "code": "P", "shares": 2000, "price": 100},
            {"date": today, "code": "S", "shares": 100, "price": 100},
        ])
        with connect(tmp_db) as conn:
            data = get_recent_insider_activity(conn, "AAPL")
        assert data["recent_buys"] == 3
        assert data["recent_sells"] == 1
        assert data["net_direction"] == "buying"
        assert data["total_buy_value"] == 1_000_000.0  # 5k+3k+2k @ 100
        assert data["total_sell_value"] == 10_000.0

    def test_notable_picks_biggest_buy(self, tmp_db):
        from datetime import date
        today = date.today().isoformat()
        self._seed_transactions(tmp_db, "AAPL", "0000320193", [
            {"date": today, "code": "P", "shares": 100, "price": 100,
             "owner": "SMALL FRY"},
            {"date": today, "code": "P", "shares": 50000, "price": 100,
             "owner": "COOK TIMOTHY D", "officer": True, "title": "CEO"},
        ])
        with connect(tmp_db) as conn:
            data = get_recent_insider_activity(conn, "AAPL")
        assert "COOK TIMOTHY D" in data["notable"]
        assert "5,000,000" in data["notable"]  # 50k * 100
        assert "CEO" in data["notable"]

    def test_cluster_counts_distinct_buyers_in_last_14d(self, tmp_db):
        from datetime import date
        today = date.today().isoformat()
        self._seed_transactions(tmp_db, "AAPL", "0000320193", [
            {"date": today, "code": "P", "owner": "INSIDER_A"},
            {"date": today, "code": "P", "owner": "INSIDER_B"},
            {"date": today, "code": "P", "owner": "INSIDER_A"},  # dup
            {"date": today, "code": "P", "owner": "INSIDER_C"},
            {"date": today, "code": "S", "owner": "INSIDER_D"},  # not a buy
        ])
        with connect(tmp_db) as conn:
            data = get_recent_insider_activity(conn, "AAPL")
        assert data["cluster_count"] == 3  # A, B, C — D excluded (sell)

    def test_only_P_and_S_codes_counted(self, tmp_db):
        from datetime import date
        today = date.today().isoformat()
        # A (Award), M (Conversion), F (Tax) are NOT buy/sell signals.
        self._seed_transactions(tmp_db, "AAPL", "0000320193", [
            {"date": today, "code": "A", "shares": 1000, "price": 100},
            {"date": today, "code": "M", "shares": 1000, "price": 100},
            {"date": today, "code": "F", "shares": 1000, "price": 100},
        ])
        with connect(tmp_db) as conn:
            data = get_recent_insider_activity(conn, "AAPL")
        assert data["recent_buys"] == 0
        assert data["recent_sells"] == 0
        assert data["total_buy_value"] == 0.0

    def test_old_txns_excluded_by_lookback(self, tmp_db):
        from datetime import date, timedelta
        old = (date.today() - timedelta(days=180)).isoformat()
        recent = date.today().isoformat()
        self._seed_transactions(tmp_db, "AAPL", "0000320193", [
            {"date": old, "code": "P", "shares": 9999, "price": 1},
            {"date": recent, "code": "P", "shares": 100, "price": 1},
        ])
        with connect(tmp_db) as conn:
            data = get_recent_insider_activity(conn, "AAPL", lookback_days=90)
        # Only the recent 100 counts.
        assert data["recent_buys"] == 1
        assert data["total_buy_value"] == 100.0


class TestNoCikTickers:
    """2026-05-17: persist tickers known to have no CIK so daily
    scrape skips them quietly instead of generating 'ticker error'
    every day for a stable population (ETFs, delisted, foreign)."""

    def test_mark_and_lookup(self, tmp_db):
        from edgar_form4.store import mark_no_cik, is_known_no_cik
        with connect(tmp_db) as conn:
            assert is_known_no_cik(conn, "ANSS") is False
            mark_no_cik(conn, "ANSS", reason="acquired by Synopsys")
            conn.commit()
            assert is_known_no_cik(conn, "ANSS") is True
            assert is_known_no_cik(conn, "anss") is True  # case-insensitive

    def test_clear_removes(self, tmp_db):
        from edgar_form4.store import (
            mark_no_cik, is_known_no_cik, clear_known_no_cik,
        )
        with connect(tmp_db) as conn:
            mark_no_cik(conn, "ANSS")
            conn.commit()
            clear_known_no_cik(conn, "ANSS")
            conn.commit()
            assert is_known_no_cik(conn, "ANSS") is False

    def test_mark_is_idempotent(self, tmp_db):
        """Re-marking an existing ticker updates last_checked_at,
        doesn't error or duplicate."""
        from edgar_form4.store import (
            mark_no_cik, list_known_no_cik,
        )
        with connect(tmp_db) as conn:
            mark_no_cik(conn, "ANSS")
            mark_no_cik(conn, "ANSS")
            mark_no_cik(conn, "ANSS")
            conn.commit()
            rows = list_known_no_cik(conn)
        assert len(rows) == 1
        assert rows[0]["ticker"] == "ANSS"

    def test_list_returns_all_in_alpha_order(self, tmp_db):
        from edgar_form4.store import mark_no_cik, list_known_no_cik
        with connect(tmp_db) as conn:
            for tk in ("CEIX", "ANSS", "BITF"):
                mark_no_cik(conn, tk)
            conn.commit()
            rows = list_known_no_cik(conn)
        assert [r["ticker"] for r in rows] == ["ANSS", "BITF", "CEIX"]


class TestOrphanedRunSweep:
    """2026-05-16: prod had a scrape_runs row stuck status='running'
    since 19:17 because the process was killed mid-run. The sweep at
    the top of start_run cleans up zombies older than 6h on each
    fresh invocation."""

    def test_old_running_row_gets_killed_on_next_start_run(self, tmp_db):
        from edgar_form4.store import start_run
        # Seed a zombie row dated 8 hours ago.
        with connect(tmp_db) as conn:
            conn.execute(
                "INSERT INTO scrape_runs (source, started_at, status) "
                "VALUES ('zombie', datetime('now', '-8 hours'), 'running')"
            )
            conn.commit()
        # Next start_run should sweep it.
        with connect(tmp_db) as conn:
            new_id = start_run(conn, "fresh:test")
            conn.commit()
            zombie = conn.execute(
                "SELECT status, finished_at, error FROM scrape_runs "
                "WHERE source='zombie'"
            ).fetchone()
        assert zombie["status"] == "killed", (
            f"old running row should have been swept to 'killed'; "
            f"got status={zombie['status']!r}"
        )
        assert zombie["finished_at"], "killed row needs finished_at"
        assert "auto-marked killed" in (zombie["error"] or ""), (
            "killed row needs a diagnostic note explaining the sweep"
        )
        assert new_id > 0  # fresh run still got its own id

    def test_recent_running_row_is_NOT_swept(self, tmp_db):
        """A run that legitimately is still in progress (started <6h
        ago) must not get killed by the sweep — that would prevent
        legitimate long-running scrapes from ever finishing."""
        from edgar_form4.store import start_run
        with connect(tmp_db) as conn:
            conn.execute(
                "INSERT INTO scrape_runs (source, started_at, status) "
                "VALUES ('in_progress', datetime('now', '-1 hours'), "
                "        'running')"
            )
            conn.commit()
        with connect(tmp_db) as conn:
            start_run(conn, "another:test")
            conn.commit()
            still_running = conn.execute(
                "SELECT status FROM scrape_runs WHERE source='in_progress'"
            ).fetchone()
        assert still_running["status"] == "running"

    def test_ok_rows_never_touched_by_sweep(self, tmp_db):
        from edgar_form4.store import start_run
        with connect(tmp_db) as conn:
            conn.execute(
                "INSERT INTO scrape_runs (source, started_at, finished_at, "
                "                          status, rows_inserted) "
                "VALUES ('done', datetime('now', '-2 days'), "
                "         datetime('now', '-2 days'), 'ok', 5)"
            )
            conn.commit()
        with connect(tmp_db) as conn:
            start_run(conn, "fresh:test")
            conn.commit()
            done = conn.execute(
                "SELECT status FROM scrape_runs WHERE source='done'"
            ).fetchone()
        assert done["status"] == "ok"
