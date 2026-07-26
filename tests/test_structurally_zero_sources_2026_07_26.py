"""Structurally-zero sources reconnected + constant meta features
killed (P1.8, 2026-07-26).

Five alt-data sources returned identical zero payloads for EVERY
symbol, each for a mundane, silent reason:

- finra_short_vol: FINRA now publishes DECIMAL volumes (fractional
  shares); int() raised ValueError on every row of every date.
- star_manager_holdings: joined h.filing_id = f.id — neither column
  exists (real key: accession_number); also filing_date vs
  filed_date. OperationalError on every call.
- insider_track_records: selected insider_name / transaction_code /
  ticker from a table whose real columns are rpt_owner_name /
  txn_code / cik (no ticker — companies join required).
- risk_factor_diff: read primaryDocumentUrl/url keys that
  get_company_filings never returns (real: primary_doc_url); also
  filing_date vs filed_date.
- activist_13dg: the filing-page regexes expected CIK-then-
  "(Subject)" but EDGAR renders NAME (Subject) ... CIK — never
  matched once; every stored row had empty subject fields and the
  reader matches on subject_ticker.

Plus the meta-feature layer: congress_direction read a bundle key
that never existed; reddit_* are credential-gated off and were
constant 0; app_store ranks fed a 999 sentinel to the GBM as a real
value. And BOTH cache layers cached empty payloads at full TTL, so
transient blips poisoned (symbol, source) pairs for days.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

import alternative_data as ad  # noqa: E402
import altdata_tier3 as t3  # noqa: E402


class TestFinraDecimalVolumes:
    def test_parses_decimal_volume_rows(self):
        body = ("Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market\n"
                "20260724|AAPL|9492768.874270|1234.5|20114433.905343|B,Q,N\n")

        def fake_urlopen(req, timeout=None):
            return io.BytesIO(body.encode())

        with patch.object(ad, "_get_cached", return_value=None), \
             patch.object(ad, "_set_cached"), \
             patch.object(ad, "urlopen", side_effect=fake_urlopen):
            out = ad.get_finra_short_volume("AAPL")
        assert out["short_volume"] == 9492768, (
            "decimal FINRA volumes must parse — int() on "
            "'9492768.874270' is how this source read zero for every "
            "symbol forever."
        )
        assert out["total_volume"] == 20114433
        assert out["short_volume_ratio"] == 0.472
        assert out["date"] is not None


def _edgar13f_tmp():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE filings (accession_number TEXT PRIMARY KEY,
            cik TEXT, period_of_report TEXT, filed_date TEXT,
            total_value_usd REAL, total_positions INTEGER,
            parser_version TEXT, fetched_at TEXT);
        CREATE TABLE holdings (id INTEGER PRIMARY KEY,
            accession_number TEXT, cusip TEXT, ticker TEXT,
            company_name TEXT, class_title TEXT, shares REAL,
            value_usd REAL, put_call TEXT, investment_discretion TEXT,
            parser_version TEXT);
    """)
    conn.execute("INSERT INTO filings VALUES ('acc-1', '0001067983', "
                 "'2026-03-31', '2026-05-15', 0, 0, '', '')")
    conn.execute("INSERT INTO holdings (accession_number, cusip, ticker, "
                 "company_name, class_title) VALUES "
                 "('acc-1', '037833100', 'AAPL', 'APPLE INC', 'COM')")
    conn.commit()
    conn.close()
    return path


class TestStarManagerHoldings:
    def test_real_schema_join_finds_holder(self):
        path = _edgar13f_tmp()
        try:
            with patch.object(t3, "_edgar13f_db_path", return_value=path), \
                 patch.object(t3, "_cached", return_value=None), \
                 patch.object(t3, "_cache_put"):
                out = t3.get_star_manager_holdings("AAPL")
            assert out["has_data"] and out["count"] == 1, (
                "the accession_number join found nothing — the "
                "filing_id/id phantom-column query is back."
            )
            assert out["holders"][0]["manager"] == "Berkshire Hathaway"
            assert out["holders"][0]["as_of"] == "2026-05-15"
        finally:
            os.unlink(path)


def _form4_tmp():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE companies (cik TEXT PRIMARY KEY, ticker TEXT,
            name TEXT, last_filings_check TEXT, fetched_at TEXT);
        CREATE TABLE insider_txns (id INTEGER PRIMARY KEY,
            accession_number TEXT, cik TEXT, rpt_owner_name TEXT,
            is_officer INTEGER, is_director INTEGER,
            is_ten_percent INTEGER, officer_title TEXT,
            transaction_date TEXT, txn_code TEXT, shares REAL,
            price_per_share REAL);
    """)
    conn.execute("INSERT INTO companies VALUES "
                 "('0000320193', 'AAPL', 'Apple Inc', '', '')")
    for code in ("S", "S", "P"):
        conn.execute(
            "INSERT INTO insider_txns (cik, rpt_owner_name, txn_code) "
            "VALUES ('0000320193', 'COOK TIMOTHY D', ?)", (code,))
    conn.commit()
    conn.close()
    return path


class TestInsiderTrackRecords:
    def test_real_schema_finds_named_insiders(self):
        path = _form4_tmp()
        try:
            with patch.object(t3, "_form4_db_path", return_value=path), \
                 patch.object(t3, "_cached", return_value=None), \
                 patch.object(t3, "_cache_put"):
                out = t3.get_insider_track_records("AAPL")
            assert out["has_data"], (
                "the companies-join query found nothing — the "
                "insider_name/transaction_code/ticker phantom-column "
                "query is back."
            )
            top = out["top_insiders"][0]
            assert top["name"] == "COOK TIMOTHY D"
            assert top["buys"] == 1 and top["sells"] == 2
        finally:
            os.unlink(path)


class TestRiskFactorDiff:
    def test_uses_real_filing_keys(self):
        filings = [
            {"accession_number": "a1", "form_type": "10-K",
             "filed_date": "2025-10-31",
             "primary_doc_url": "https://sec.gov/cur.htm"},
            {"accession_number": "a0", "form_type": "10-K",
             "filed_date": "2024-11-01",
             "primary_doc_url": "https://sec.gov/pri.htm"},
        ]
        texts = {
            "https://sec.gov/cur.htm":
                "We may face new tariffs. We may lose key personnel.",
            "https://sec.gov/pri.htm":
                "We may lose key personnel.",
        }
        import sec_filings
        with patch.object(t3, "_cached", return_value=None), \
             patch.object(t3, "_cache_put"), \
             patch.object(sec_filings, "get_company_filings",
                          return_value=filings), \
             patch.object(sec_filings, "fetch_filing_text",
                          side_effect=lambda u: texts.get(u, "")):
            out = t3.get_risk_factor_diff("AAPL")
        assert out["has_new_risks"] and out["added_risk_count"] >= 1, (
            "the diff saw no text — the primaryDocumentUrl/url "
            "phantom keys are back (real key: primary_doc_url)."
        )
        assert out["latest_filing_date"] == "2025-10-31"


class Test13dgSubjectExtraction:
    _PAGE = ('<div class="companyInfo"><span class="companyName">'
             'GENCO SHIPPING &amp; TRADING LTD (Subject)  '
             '<acronym title="Central Index Key">CIK</acronym>: '
             '<a href="/cgi-bin/browse-edgar?action=getcompany&'
             'CIK=0001326200&type=SC+13D">0001326200 (see all)</a></span></div>')

    def test_extracts_from_real_page_structure(self):
        import sec_13dg_activist as s13
        import sec_filings
        with patch.object(sec_filings, "_rate_limited_get",
                          return_value=self._PAGE.encode()):
            sub = s13._extract_subject_from_filing_page("http://x")
        assert sub == {"subject_name": "GENCO SHIPPING & TRADING LTD",
                       "subject_cik": "0001326200"}, (
            "the page renders NAME (Subject) ... CIK — the inverted "
            "CIK-then-(Subject) regexes matched nothing, ever."
        )

    def test_url_path_cik_fallback(self):
        import sec_13dg_activist as s13
        m = s13._URL_CIK_RE.search(
            "https://www.sec.gov/Archives/edgar/data/1326200/000110465926082921/x-index.htm")
        assert m and m.group(1).zfill(10) == "0001326200"

    def test_reader_matches_backfilled_rows(self):
        import sec_13dg_activist as s13
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(path)
        try:
            s13._ensure_13dg_table(path)
            conn = sqlite3.connect(path)
            conn.execute(
                "INSERT INTO recent_13dg_filings (accession, form_type, "
                "filing_date, subject_name, subject_cik, subject_ticker, "
                "source_url) VALUES ('acc-x', 'SC 13D', date('now'), "
                "'GENCO SHIPPING & TRADING LTD', '0001326200', 'GNK', 'u')")
            conn.commit()
            conn.close()
            with patch.object(s13, "_altdata_db_path", return_value=path):
                out = s13.get_recent_13dg_activist("GNK")
            assert out["count"] == 1 and out["has_13d"]
        finally:
            os.unlink(path)


class TestMetaFeatureHonesty:
    def test_congress_direction_reads_real_bundle_key(self):
        src = open(os.path.join(REPO, "trade_pipeline.py")).read()
        assert 'alt.get("congressional", {})' not in src, (
            "congress_direction is reading the phantom 'congressional' "
            "key again — the bundle key is congressional_recent."
        )
        assert 'alt.get("congressional_recent") or {}' in src

    def test_reddit_features_dropped_from_model(self):
        from meta_model import NUMERIC_FEATURES
        assert "reddit_mentions" not in NUMERIC_FEATURES
        assert "reddit_sentiment" not in NUMERIC_FEATURES, (
            "reddit features are credential-gated OFF and constant 0 "
            "— they dilute the model. Re-add only with credentials."
        )

    def test_app_store_sentinel_replaced_by_scores(self):
        from meta_model import NUMERIC_FEATURES
        assert "app_store_grossing_score" in NUMERIC_FEATURES
        assert "app_store_grossing_rank" not in NUMERIC_FEATURES
        src = open(os.path.join(REPO, "trade_pipeline.py")).read()
        assert 'or 999' not in src, (
            "a 999 sentinel is being fed to the model as a real "
            "value again — unranked must be an honest 0-score."
        )


class TestNegativeCaching:
    def test_empty_payload_gets_short_ttl(self):
        import alt_data_cache as adc
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(path)
        with patch.object(adc, "_CACHE_DB", path):
            adc._ensure_cache_db()
            adc.cache_set("AAPL", "insider", {})
            adc.cache_set("AAPL", "fundamentals", {"pe_trailing": 30.0})
            conn = sqlite3.connect(path)
            rows = dict(conn.execute(
                "SELECT source, ttl_seconds FROM altdata_cache").fetchall())
            conn.close()
        os.unlink(path)
        assert rows["insider"] <= adc.NEGATIVE_CACHE_TTL_SECONDS, (
            "an empty payload cached at full TTL — one transient blip "
            "poisons the symbol for days again."
        )
        assert rows["fundamentals"] > adc.NEGATIVE_CACHE_TTL_SECONDS

    def test_inner_layer_refuses_empty(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(path)
        try:
            with patch.object(ad, "_DB_PATH", path), \
                 patch.object(ad, "_table_ensured", False):
                ad._set_cached("p18_empty", {})
                ad._set_cached("p18_real", {"has_data": False})
                conn = sqlite3.connect(path)
                keys = [r[0] for r in conn.execute(
                    "SELECT cache_key FROM alt_data_cache").fetchall()]
                conn.close()
            assert "p18_empty" not in keys, (
                "the inner cache stored a bare {} — with no per-row "
                "TTL it self-perpetuates for the full type TTL and "
                "re-seeds the outer layer."
            )
            assert "p18_real" in keys  # marker dicts cache normally
        finally:
            if os.path.exists(path):
                os.unlink(path)


class TestCpiDateAware:
    def test_yoy_survives_missing_observation(self):
        import macro_data as md
        obs = [{"date": "2026-06-01", "value": "332.568"},
               {"date": "2026-05-01", "value": "."},  # FRED gap marker
               {"date": "2026-04-01", "value": "330.293"},
               {"date": "2025-06-01", "value": "322.169"}]

        def fake_urlopen(req, timeout=None):
            return io.BytesIO(json.dumps({"observations": obs}).encode())

        with patch.object(md, "urlopen", side_effect=fake_urlopen):
            pairs = md._fred_fetch_with_dates("CPIAUCSL", limit=18)
        assert len(pairs) == 3  # "." skipped
        assert pairs[0] == ("2026-06-01", 332.568)
        # YoY = 332.568/322.169 - 1 = ~3.2% — the date-pair the
        # fixed-index math missed whenever any month was "."
        yoy = round((pairs[0][1] / pairs[-1][1] - 1) * 100, 1)
        assert yoy == 3.2
