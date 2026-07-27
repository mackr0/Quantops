"""Alt-data reconnect (P1.1, 2026-07-26).

The connections audit found the three local scraper DBs were nearly
invisible to the per-symbol readers:

1. edgar13f: scraper read submissions-JSON key "periodOfReport" but
   SEC calls it "reportDate" — the defensive '' fallback silently
   left period_of_report empty on all 581 filings, and the reader's
   no-period fallback then summed holdings across every quarter
   (multi-year double count). holdings.ticker was populated on 1.2%
   of rows (35-CUSIP hand seed).
2. biotechevents: trials.ticker populated on 6.4% (hand sponsor map).
3. stocktwits: watchlist was 37 tickers vs a ~400-symbol trading
   universe, and the stocktwits_data_absent specialist read our own
   coverage gap as a bearish "no retail attention" signal.

Fixes pinned here: the reportDate key, the enrichment module
(altdata_enrich.py: SEC-registrant name matching, equity-class-only
stamping, period backfill), the per-filer-latest reader fallback,
the covered flag, the specialist guard, and the universe watchlist.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)
for _proj in ("edgar13f", "stocktwits"):
    _p = os.path.join(REPO, "altdata", _proj)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import altdata_enrich as ae  # noqa: E402


# ─────────────────────────────────────────────────────────────────────
# 1. The reportDate key fix in the edgar13f scraper
# ─────────────────────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, payload):
        self._payload = payload

    def get(self, url, **kw):
        return _FakeResp(self._payload)


class TestScraperPeriodKey:
    def test_report_date_key_is_read(self):
        from edgar13f import scrape as e13f_scrape
        payload = {"filings": {"recent": {
            "form": ["13F-HR", "10-K"],
            "accessionNumber": ["0001-26-000001", "0001-26-000002"],
            "reportDate": ["2026-03-31", "2025-12-31"],
            "filingDate": ["2026-05-10", "2026-02-20"],
            "primaryDocument": ["primary_doc.xml", "x.htm"],
        }}}
        out = e13f_scrape.list_13f_filings_for_filer(
            _FakeSession(payload), "0000000001")
        assert len(out) == 1
        assert out[0]["period_of_report"] == "2026-03-31", (
            "list_13f_filings_for_filer no longer reads the SEC "
            "'reportDate' field — every filing will be stored with "
            "an empty period again."
        )


# ─────────────────────────────────────────────────────────────────────
# 2. Name normalization + registrant matching
# ─────────────────────────────────────────────────────────────────────

class TestNameNormalization:
    def test_suffixes_and_punctuation(self):
        f = ae.normalize_company_name
        assert f("COINBASE GLOBAL INC") == "COINBASE GLOBAL"
        assert f("Coinbase Global, Inc.") == "COINBASE GLOBAL"
        assert f("Zimmer Biomet Holdings, Inc.") == "ZIMMER BIOMET"
        assert f("Johnson & Johnson") == "JOHNSON AND JOHNSON"
        assert f("ACADIA Pharmaceuticals Inc.") == "ACADIA PHARMACEUTICALS"
        assert f(None) == ""
        assert f("  ") == ""

    def test_equity_class_filter(self):
        yes = ["COM", "Cmn", "COM NEW", "CL A", "COM CL A", "SHS",
               "SPONSORED ADR", "SPONSORED ADS", "ORD SHS",
               "Depository Receipt", "COMMON STOCK", "CLASS A COM"]
        no = ["Bond", "NOTE 1.375%", "CONV NOTE", "PFD", "PREF SER A",
              "WT", "WARRANT", "UNIT", "DEBENTURE", "", None]
        for t in yes:
            assert ae._is_equity_class(t), t
        for t in no:
            assert not ae._is_equity_class(t), t

    def test_sponsor_aliases_and_unique_match(self):
        idx = {"VIR BIOTECHNOLOGY": ["VIR"],
               "ALPHABET": ["GOOG", "GOOGL"]}
        assert ae.sponsor_ticker_for(
            "Vir Biotechnology, Inc.", idx) == "VIR"
        assert ae.sponsor_ticker_for(
            "Seagen, a wholly owned subsidiary of Pfizer", idx) == "PFE"
        assert ae.sponsor_ticker_for("Celgene", idx) == "BMY"
        # ambiguous (share classes) must not guess
        assert ae.sponsor_ticker_for("Alphabet Inc.", idx) is None
        assert ae.sponsor_ticker_for("Beijing Sport University", idx) is None


# ─────────────────────────────────────────────────────────────────────
# 3. Enrichment against temp DBs
# ─────────────────────────────────────────────────────────────────────

def _mk_13f_db(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE filers (cik TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE filings (
            accession_number TEXT PRIMARY KEY, cik TEXT,
            period_of_report TEXT, filed_date TEXT);
        CREATE TABLE holdings (
            id INTEGER PRIMARY KEY, accession_number TEXT, cusip TEXT,
            ticker TEXT, company_name TEXT, class_title TEXT,
            shares REAL, value_usd REAL, put_call TEXT);
    """)
    return conn


NAME_IDX = {
    "COINBASE GLOBAL": ["COIN"],
    "ALPHABET": ["GOOG", "GOOGL"],
    "NVIDIA": ["NVDA"],
}


class TestEnrich13fTickers:
    def test_stamps_equity_skips_debt_and_ambiguous(self, tmp_path):
        conn = _mk_13f_db(str(tmp_path / "e.db"))
        rows = [
            # equity, unique name -> stamped
            ("a1", "19260Q107", "", "COINBASE GLOBAL INC", "COM", 100),
            # debt cusip of the same issuer -> NOT stamped
            ("a1", "19260QAB3", "", "COINBASE GLOBAL INC", "Bond", 50),
            # ambiguous share classes -> NOT stamped
            ("a1", "02079K305", "", "ALPHABET INC", "CL A", 10),
            # already stamped -> untouched
            ("a1", "67066G104", "NVDA", "NVIDIA CORP", "COM", 5),
        ]
        for acc, cusip, tick, name, cls, sh in rows:
            conn.execute(
                "INSERT INTO holdings (accession_number, cusip, ticker, "
                "company_name, class_title, shares) VALUES (?,?,?,?,?,?)",
                (acc, cusip, tick, name, cls, sh))
        conn.commit()
        updated, skipped = ae.enrich_13f_tickers(conn, name_index=NAME_IDX)
        got = dict(conn.execute(
            "SELECT cusip, COALESCE(ticker,'') FROM holdings"))
        assert got["19260Q107"] == "COIN"
        assert got["19260QAB3"] == "", (
            "a Bond-class CUSIP was ticker-stamped — debt quantities "
            "will pollute the reader's share aggregates.")
        assert got["02079K305"] == "", "ambiguous name must not be guessed"
        assert got["67066G104"] == "NVDA"
        assert updated == 1 and skipped >= 1
        # idempotent
        updated2, _ = ae.enrich_13f_tickers(conn, name_index=NAME_IDX)
        assert updated2 == 0

    def test_conflicting_names_for_one_cusip_not_stamped(self, tmp_path):
        conn = _mk_13f_db(str(tmp_path / "e2.db"))
        for name, tick_idx in (("COINBASE GLOBAL INC", None),
                               ("NVIDIA CORP", None)):
            conn.execute(
                "INSERT INTO holdings (accession_number, cusip, ticker, "
                "company_name, class_title, shares) "
                "VALUES ('a1', 'DEADBEEF1', '', ?, 'COM', 1)", (name,))
        conn.commit()
        updated, _ = ae.enrich_13f_tickers(conn, name_index=NAME_IDX)
        assert updated == 0, (
            "two filings named different issuers for one CUSIP — "
            "stamping either would be a guess.")


class TestEnrich13fPeriods:
    def test_backfills_only_empty(self, tmp_path):
        conn = _mk_13f_db(str(tmp_path / "p.db"))
        conn.execute("INSERT INTO filings VALUES "
                     "('0001-26-000001', '123', '', '2026-05-10')")
        conn.execute("INSERT INTO filings VALUES "
                     "('0001-26-000002', '123', '2025-12-31', '2026-02-10')")
        conn.commit()
        payload = json.dumps({"filings": {"recent": {
            "accessionNumber": ["0001-26-000001", "0001-26-000002"],
            "reportDate": ["2026-03-31", "1999-01-01"],
        }}}).encode()
        with patch.object(ae, "_fetch_url", return_value=payload), \
             patch.object(ae.time, "sleep"):
            n = ae.enrich_13f_periods(conn)
        assert n == 1
        got = dict(conn.execute(
            "SELECT accession_number, period_of_report FROM filings"))
        assert got["0001-26-000001"] == "2026-03-31"
        assert got["0001-26-000002"] == "2025-12-31", (
            "an already-populated period_of_report was overwritten")


class TestEnrichBiotech:
    def test_industry_sponsors_stamped_universities_untouched(
            self, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "b.db"))
        conn.executescript("""
            CREATE TABLE trials (nct_id TEXT PRIMARY KEY,
                sponsor_name TEXT, sponsor_class TEXT, ticker TEXT);
        """)
        rows = [
            ("n1", "Vir Biotechnology, Inc.", "INDUSTRY", ""),
            ("n2", "Seagen, a wholly owned subsidiary of Pfizer",
             "INDUSTRY", ""),
            ("n3", "Beijing Sport University", "OTHER", ""),
            ("n4", "Alphabet Inc.", "INDUSTRY", ""),   # ambiguous
            ("n5", "Merck Sharp & Dohme LLC", "INDUSTRY", "MRK"),
        ]
        conn.executemany("INSERT INTO trials VALUES (?,?,?,?)", rows)
        conn.commit()
        idx = {"VIR BIOTECHNOLOGY": ["VIR"],
               "ALPHABET": ["GOOG", "GOOGL"]}
        n = ae.enrich_biotech_tickers(conn, name_index=idx)
        got = dict(conn.execute(
            "SELECT nct_id, COALESCE(ticker,'') FROM trials"))
        assert got == {"n1": "VIR", "n2": "PFE", "n3": "",
                       "n4": "", "n5": "MRK"}
        assert n == 2
        assert ae.enrich_biotech_tickers(conn, name_index=idx) == 0


# ─────────────────────────────────────────────────────────────────────
# 4. Reader fixes (alternative_data)
# ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def altdata_base(tmp_path, monkeypatch):
    base = tmp_path / "altdata"
    for proj in ("edgar13f", "stocktwits"):
        (base / proj / "data").mkdir(parents=True)
    monkeypatch.setenv("ALTDATA_BASE_PATH", str(base))
    import alternative_data as ad
    # the readers memo-cache results in-process — isolate per test
    monkeypatch.setattr(ad, "_ALT_CACHE", {}, raising=False)
    return base


def _reader_13f_db(base):
    conn = sqlite3.connect(str(base / "edgar13f" / "data" / "edgar13f.db"))
    conn.executescript("""
        CREATE TABLE filers (cik TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE filings (
            accession_number TEXT PRIMARY KEY, cik TEXT,
            period_of_report TEXT, filed_date TEXT);
        CREATE TABLE holdings (
            id INTEGER PRIMARY KEY, accession_number TEXT, cusip TEXT,
            ticker TEXT, company_name TEXT, class_title TEXT,
            shares REAL, value_usd REAL, put_call TEXT);
    """)
    return conn


class TestReader13fNoDoubleCount:
    def _seed(self, conn, with_periods):
        # THREE quarters — two priors are load-bearing: the original
        # QoQ query summed shares across ALL prior quarters (SUM
        # collapses to one row, so its ORDER BY/LIMIT was a no-op) and
        # a single prior quarter cannot distinguish that bug from the
        # fix.
        p0, p1, p2 = (("2025-09-30", "2025-12-31", "2026-03-31")
                      if with_periods else ("", "", ""))
        conn.execute("INSERT INTO filers VALUES ('111', 'Filer A')")
        conn.execute("INSERT INTO filings VALUES ('acc-older', '111', ?, "
                     "'2025-11-10')", (p0,))
        conn.execute("INSERT INTO filings VALUES ('acc-old', '111', ?, "
                     "'2026-02-10')", (p1,))
        conn.execute("INSERT INTO filings VALUES ('acc-new', '111', ?, "
                     "'2026-05-10')", (p2,))
        for acc, sh in (("acc-older", 5000), ("acc-old", 1000),
                        ("acc-new", 400)):
            conn.execute(
                "INSERT INTO holdings (accession_number, cusip, ticker, "
                "company_name, class_title, shares, value_usd, put_call) "
                "VALUES (?, 'C1', 'COIN', 'COINBASE GLOBAL INC', 'COM', "
                "?, ?, '')", (acc, sh, sh * 100))
        conn.commit()

    def test_no_period_fallback_uses_latest_filing_only(self, altdata_base):
        conn = _reader_13f_db(altdata_base)
        self._seed(conn, with_periods=False)
        conn.close()
        import alternative_data as ad
        with patch.object(ad, "_get_cached", return_value=None), \
             patch.object(ad, "_set_cached"):
            out = ad.get_13f_institutional("COIN")
        assert out["total_shares"] == 400, (
            "the no-period fallback summed holdings across multiple "
            "filings again — multi-quarter double counting is back.")
        assert out["total_holders"] == 1

    def test_period_path_uses_latest_quarter(self, altdata_base):
        conn = _reader_13f_db(altdata_base)
        self._seed(conn, with_periods=True)
        conn.close()
        import alternative_data as ad
        with patch.object(ad, "_get_cached", return_value=None), \
             patch.object(ad, "_set_cached"):
            out = ad.get_13f_institutional("COIN")
        assert out["quarter"] == "2026-03-31"
        assert out["total_shares"] == 400
        # QoQ compares against the IMMEDIATELY preceding quarter
        # (1000), not the sum of all priors (6000 -> -93.3%, the
        # every-symbol-reads-≈-100% bug).
        assert out["qoq_share_change_pct"] == -60.0


class TestStocktwitsCoveredFlag:
    def _seed(self, base):
        conn = sqlite3.connect(
            str(base / "stocktwits" / "data" / "stocktwits.db"))
        conn.executescript("""
            CREATE TABLE ticker_sentiment_daily (
                ticker TEXT, date TEXT, n_messages INT, n_bullish INT,
                n_bearish INT, n_neutral INT, net_sentiment REAL);
            CREATE TABLE trending_snapshots (
                ticker TEXT, rank INT, snapshot_at TEXT);
        """)
        conn.execute(
            "INSERT INTO ticker_sentiment_daily VALUES "
            "('AAPL', date('now'), 12, 8, 2, 2, 0.5)")
        # covered but currently quiet
        conn.execute(
            "INSERT INTO ticker_sentiment_daily VALUES "
            "('WOLF', date('now', '-90 days'), 3, 1, 1, 1, 0.0)")
        conn.commit()
        conn.close()

    def test_covered_vs_untracked(self, altdata_base):
        self._seed(altdata_base)
        import alternative_data as ad
        with patch.object(ad, "_get_cached", return_value=None), \
             patch.object(ad, "_set_cached"):
            aapl = ad.get_stocktwits_sentiment("AAPL")
            wolf = ad.get_stocktwits_sentiment("WOLF")
            zzzz = ad.get_stocktwits_sentiment("ZZZZ")
        assert aapl["covered"] is True and aapl["message_count_7d"] == 12
        assert wolf["covered"] is True and wolf["message_count_7d"] == 0
        assert zzzz["covered"] is False, (
            "an untracked symbol reports covered=True — downstream "
            "will read the coverage gap as a real absence signal.")


class TestAltdataQueryLogging:
    def test_schema_error_warns(self, altdata_base, caplog):
        conn = sqlite3.connect(
            str(altdata_base / "stocktwits" / "data" / "stocktwits.db"))
        conn.execute("CREATE TABLE unrelated (x)")
        conn.commit()
        conn.close()
        import alternative_data as ad
        with caplog.at_level(logging.WARNING, logger=ad.logger.name):
            rows = ad._altdata_query(
                "stocktwits", "stocktwits.db",
                "SELECT * FROM ticker_sentiment_daily")
        assert rows == []
        assert any("altdata query failed" in r.message
                   for r in caplog.records), (
            "a query failure against an existing DB no longer warns — "
            "silent alt-data breakage is how P1.1 happened.")

    def test_missing_db_stays_silent(self, altdata_base, caplog):
        import alternative_data as ad
        with caplog.at_level(logging.WARNING, logger=ad.logger.name):
            rows = ad._altdata_query(
                "nosuchproject", "x.db", "SELECT 1")
        assert rows == []
        assert not [r for r in caplog.records
                    if r.levelno >= logging.WARNING]


# ─────────────────────────────────────────────────────────────────────
# 5. Specialist coverage guard
# ─────────────────────────────────────────────────────────────────────

class TestStocktwitsAbsentSpecialist:
    def _cand(self, st):
        return {"price": 12.0,
                "alt_data": {"stocktwits_sentiment": st,
                             "insider": {"x": 1}}}

    def test_untracked_symbol_does_not_fire(self):
        from deterministic_specialists import stocktwits_data_absent as sp
        out = sp.evaluate(self._cand(
            {"covered": False, "message_count_7d": 0,
             "net_sentiment_7d": None}))
        assert out is None, (
            "the specialist fired a CAUTION off our own coverage gap "
            "(covered=False) — that is a data artifact, not a signal.")

    def test_covered_and_silent_fires(self):
        from deterministic_specialists import stocktwits_data_absent as sp
        out = sp.evaluate(self._cand(
            {"covered": True, "message_count_7d": 0,
             "net_sentiment_7d": None}))
        assert out is not None and out["severity"] == "CAUTION"

    def test_covered_with_data_does_not_fire(self):
        from deterministic_specialists import stocktwits_data_absent as sp
        out = sp.evaluate(self._cand(
            {"covered": True, "message_count_7d": 40,
             "net_sentiment_7d": 0.4}))
        assert out is None


# ─────────────────────────────────────────────────────────────────────
# 6. StockTwits watchlist = trading universe
# ─────────────────────────────────────────────────────────────────────

class TestUniverseWatchlist:
    def test_universe_watchlist_covers_stock_universe(self):
        from stocktwits import cli as st_cli
        from segments import STOCK_UNIVERSE
        wl = st_cli._universe_watchlist()
        assert set(STOCK_UNIVERSE) <= set(wl), (
            "the stocktwits watchlist no longer covers the trading "
            "universe — the 37-vs-400 coverage gap is back.")
        assert len(wl) == len(set(wl))

    def test_default_fallback_still_loads(self, tmp_path):
        from stocktwits import cli as st_cli
        with patch.object(st_cli, "_universe_watchlist",
                          side_effect=ImportError("no segments")):
            with patch.object(st_cli, "WATCHLIST_FILE",
                              Path(tmp_path) / "nope.txt"):
                wl = st_cli._load_watchlist()
        assert wl == st_cli.DEFAULT_WATCHLIST

    def test_file_override_wins(self, tmp_path):
        from stocktwits import cli as st_cli
        f = Path(tmp_path) / "wl.txt"
        f.write_text("# comment\naapl\nMSFT\n")
        with patch.object(st_cli, "WATCHLIST_FILE", f):
            assert st_cli._load_watchlist() == ["AAPL", "MSFT"]


# ─────────────────────────────────────────────────────────────────────
# 7. 404 = "symbol not on StockTwits", not a failure (2026-07-27)
# ─────────────────────────────────────────────────────────────────────

class TestStocktwits404IsInfoNotWarning:
    def test_404_logs_info_and_other_errors_warn(self, caplog):
        """The P1.1 full-universe watchlist surfaced ~23 delisted/
        renamed tickers (PTRA, APPH, SQ, DLOCAL, ...). StockTwits
        404s on them every daily run; each 404 logged at WARNING
        sprayed the errors page with non-errors. A 404 is a fact
        (symbol unknown there) → INFO; genuine failures (5xx,
        network) stay WARNING."""
        import logging
        from unittest.mock import patch
        from stocktwits import scrape as st_scrape

        with patch.object(st_scrape, "_get",
                          side_effect=Exception("404 Client Error: Not Found")):
            with caplog.at_level(logging.INFO, logger=st_scrape.logger.name):
                st_scrape.fetch_messages_for_ticker(None, "PTRA")
        recs_404 = [r for r in caplog.records if "PTRA" in r.getMessage()]
        assert recs_404 and all(r.levelno == logging.INFO for r in recs_404), (
            "a StockTwits 404 logged above INFO again — dead tickers "
            "spray the errors page every daily run."
        )

        caplog.clear()
        with patch.object(st_scrape, "_get",
                          side_effect=Exception("503 Server Error")):
            with caplog.at_level(logging.INFO, logger=st_scrape.logger.name):
                st_scrape.fetch_messages_for_ticker(None, "AAPL")
        recs_503 = [r for r in caplog.records if "AAPL" in r.getMessage()]
        assert recs_503 and all(r.levelno == logging.WARNING
                                for r in recs_503), (
            "a genuine StockTwits failure must stay WARNING."
        )
