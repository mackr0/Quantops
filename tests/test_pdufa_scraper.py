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
              "pdufa_date": "2026-08-15",
              "sponsor_company": "Bristol-Myers Squibb",
              "source_url": "https://example.com/v1.htm"}
        sync_pdufa_events_to_altdata_db([e], db_path=db)
        # Re-sync with updated source URL — should not duplicate
        # (UNIQUE on drug_name + sponsor_company + pdufa_date)
        e["source_url"] = "https://example.com/v2.htm"
        sync_pdufa_events_to_altdata_db([e], db_path=db)
        conn = sqlite3.connect(db)
        n = conn.execute("SELECT COUNT(*) FROM pdufa_events").fetchone()[0]
        urls = conn.execute(
            "SELECT source_url FROM pdufa_events"
        ).fetchall()
        conn.close()
        assert n == 1
        assert urls[0][0] == "https://example.com/v2.htm"

    def test_empty_events_writes_nothing(self, tmp_path):
        from pdufa_scraper import sync_pdufa_events_to_altdata_db
        db = str(tmp_path / "biotechevents.db")
        n = sync_pdufa_events_to_altdata_db([], db_path=db)
        assert n == 0


class TestFetchAndRunSync:
    def test_fetch_uses_fallback_on_edgar_failure(self):
        from pdufa_scraper import fetch_pdufa_events
        # EDGAR returning empty falls through to PDUFA_FALLBACK_SEED
        # (empty by default — no crash, just zero rows).
        with patch("pdufa_scraper._fetch_edgar_search", return_value={}):
            events = fetch_pdufa_events()
        assert events == []

    def test_run_full_sync_returns_counts(self, tmp_path, monkeypatch):
        from pdufa_scraper import run_full_sync
        db = str(tmp_path / "biotechevents.db")
        monkeypatch.setattr(
            "pdufa_scraper._altdata_db_path", lambda: db,
        )
        # Mock the EDGAR path with three plausible hits.
        fake_search = {
            "hits": {
                "hits": [
                    {
                        "_id": f"0001-26-{i:06d}:doc.htm",
                        "_source": {
                            "display_names": [
                                f"Co{i}  ({tkr})  (CIK 0000{i:04d})"
                            ],
                            "ciks": [f"0000{i:04d}"],
                            "adsh": f"0001-26-{i:06d}",
                        },
                    }
                    for i, tkr in enumerate(["BMY", "MRNA", "GILD"], start=1)
                ]
            }
        }
        text = "PDUFA target action date of August 15, 2026."
        with patch(
            "pdufa_scraper._fetch_edgar_search", return_value=fake_search
        ), patch(
            "pdufa_scraper._fetch_filing_text", return_value=text
        ), patch(
            "pdufa_scraper.time.sleep", lambda _: None
        ):
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


class TestEdgarTickerExtract:
    def test_typical_display_name(self):
        from pdufa_scraper import _extract_ticker_from_display
        assert _extract_ticker_from_display(
            "Merck & Co., Inc.  (MRK)  (CIK 0000310158)"
        ) == "MRK"

    def test_short_ticker(self):
        from pdufa_scraper import _extract_ticker_from_display
        assert _extract_ticker_from_display(
            "Pfizer Inc.  (PFE)  (CIK 0000078003)"
        ) == "PFE"

    def test_no_ticker(self):
        from pdufa_scraper import _extract_ticker_from_display
        assert _extract_ticker_from_display(
            "Some Private Co  (CIK 0001234567)"
        ) is None

    def test_empty(self):
        from pdufa_scraper import _extract_ticker_from_display
        assert _extract_ticker_from_display("") is None
        assert _extract_ticker_from_display(None) is None


class TestEdgarFilingURL:
    def test_constructs_archive_url(self):
        from pdufa_scraper import _build_filing_doc_url
        url = _build_filing_doc_url(
            "0001104659-26-052081", "0000310158", "tm2612241d1_ex99-1.htm"
        )
        assert url == (
            "https://www.sec.gov/Archives/edgar/data/"
            "310158/000110465926052081/tm2612241d1_ex99-1.htm"
        )

    def test_strips_cik_leading_zeros(self):
        from pdufa_scraper import _build_filing_doc_url
        url = _build_filing_doc_url(
            "0001234567-26-000001", "0000000123", "doc.htm"
        )
        assert "/123/" in url


class TestPdufaTextParsing:
    def test_extracts_long_form_date(self):
        from pdufa_scraper import _parse_pdufa_dates_from_text
        text = (
            "The Company announced that the FDA assigned a "
            "PDUFA target action date of March 15, 2027 for the "
            "review of the supplemental new drug application."
        )
        dates = _parse_pdufa_dates_from_text(text)
        assert "2027-03-15" in dates

    def test_extracts_iso_date(self):
        from pdufa_scraper import _parse_pdufa_dates_from_text
        text = "The PDUFA action date of 2026-09-30 was confirmed."
        dates = _parse_pdufa_dates_from_text(text)
        assert "2026-09-30" in dates

    def test_extracts_us_format(self):
        from pdufa_scraper import _parse_pdufa_dates_from_text
        text = "FDA accepted the BLA with PDUFA goal date of 9/30/2026."
        dates = _parse_pdufa_dates_from_text(text)
        assert "2026-09-30" in dates

    def test_no_pdufa_no_match(self):
        from pdufa_scraper import _parse_pdufa_dates_from_text
        text = "The company reported quarterly earnings on March 15, 2027."
        assert _parse_pdufa_dates_from_text(text) == []

    def test_dedupes_repeated_dates(self):
        from pdufa_scraper import _parse_pdufa_dates_from_text
        text = (
            "The PDUFA date of March 15, 2027 was set. "
            "Reaffirming the PDUFA target action date of March 15, 2027."
        )
        dates = _parse_pdufa_dates_from_text(text)
        assert dates.count("2027-03-15") == 1


class TestDrugAndActionExtraction:
    def test_drug_after_nda_for(self):
        from pdufa_scraper import _parse_drug_and_action_near_pdufa
        text = (
            "The Company announced that the FDA accepted the NDA for "
            "tebipenem HBr with a PDUFA target action date of October 30, 2026."
        )
        drug, action = _parse_drug_and_action_near_pdufa(text)
        assert "tebipenem" in drug.lower()
        assert action == "NDA"

    def test_brand_name_drug(self):
        from pdufa_scraper import _parse_drug_and_action_near_pdufa
        text = (
            "FDA accepted the BLA for Eliquis with a PDUFA goal date "
            "of June 5, 2026."
        )
        drug, action = _parse_drug_and_action_near_pdufa(text)
        assert "Eliquis" in drug
        assert action == "BLA"

    def test_snda_action_type(self):
        from pdufa_scraper import _parse_drug_and_action_near_pdufa
        text = (
            "Acceptance of sNDA for label expansion for compound XYZ-123 "
            "with PDUFA target action date of August 22, 2026."
        )
        drug, action = _parse_drug_and_action_near_pdufa(text)
        assert action in ("SNDA", "NDA")

    def test_falls_back_when_no_drug_match(self):
        from pdufa_scraper import _parse_drug_and_action_near_pdufa
        text = "Just some text mentioning PDUFA without any drug context."
        drug, action = _parse_drug_and_action_near_pdufa(text)
        assert drug == "(see filing)"

    def test_no_pdufa_in_text(self):
        from pdufa_scraper import _parse_drug_and_action_near_pdufa
        drug, action = _parse_drug_and_action_near_pdufa(
            "Company announces routine business update."
        )
        assert drug == "(see filing)"

    def test_skips_false_positives(self):
        from pdufa_scraper import _parse_drug_and_action_near_pdufa
        # "the" is in _DRUG_FP set
        text = (
            "The application for the Company's product with a PDUFA "
            "target action date of June 5, 2026..."
        )
        drug, action = _parse_drug_and_action_near_pdufa(text)
        assert drug.lower() != "the"

    def test_rejects_for_the_treatment_of_X_phrase(self):
        """Real failure case from IONS sNDA filing: greedy regex was
        capturing "the treatment of sHTG" as the drug. Banned-prefix
        rejection should send us to the suffix fallback."""
        from pdufa_scraper import _parse_drug_and_action_near_pdufa
        text = (
            "The Company announced the FDA accepted the sNDA for "
            "olezarsen for the treatment of severe hypertriglyceridemia "
            "(sHTG) with a PDUFA target action date of October 26, 2026."
        )
        drug, action = _parse_drug_and_action_near_pdufa(text)
        # We should NOT capture "the treatment of sHTG"
        assert not drug.lower().startswith("the ")
        # We should capture olezarsen (via -ersen suffix or after "for")
        assert "olezarsen" in drug.lower() or drug != "(see filing)"

    def test_who_suffix_fallback_zilganersen(self):
        """Real IONS filing has "data from pivotal study of zilganersen"
        — no NDA-for-X phrasing, but -ersen suffix matches."""
        from pdufa_scraper import _parse_drug_and_action_near_pdufa
        text = (
            "Ionis presents new data from pivotal study of zilganersen "
            "in Alexander disease (AxD). PDUFA date set for September 22, 2026."
        )
        drug, action = _parse_drug_and_action_near_pdufa(text)
        assert "zilganersen" in drug.lower()

    def test_who_suffix_fallback_pembrolizumab(self):
        from pdufa_scraper import _parse_drug_and_action_near_pdufa
        text = (
            "Trial results for pembrolizumab in lung cancer. "
            "PDUFA target action date of June 5, 2026."
        )
        drug, action = _parse_drug_and_action_near_pdufa(text)
        assert "pembrolizumab" in drug.lower()

    def test_compound_code_fallback(self):
        from pdufa_scraper import _parse_drug_and_action_near_pdufa
        text = (
            "Phase 3 results for ARV-471 in metastatic breast cancer. "
            "PDUFA target action date of June 5, 2026."
        )
        drug, action = _parse_drug_and_action_near_pdufa(text)
        assert drug == "ARV-471"

    def test_rejects_sec_exhibit_marker(self):
        """EX-99.1 etc are SEC filing artifacts — not drugs."""
        from pdufa_scraper import _parse_drug_and_action_near_pdufa
        text = (
            "EX-99.1 Press Release. The Company announced PDUFA target "
            "action date of June 5, 2026 for its lead candidate."
        )
        drug, action = _parse_drug_and_action_near_pdufa(text)
        assert "EX-" not in drug
        assert "ex-" not in drug.lower()

    # The following tests use real 8-K filing snippets pulled from prod
    # 2026-05-04. Each represents a distinct phrasing the AI's biotech
    # signal would otherwise miss (drug = "(see filing)").

    def test_real_aldx_the_drug_NDA_pattern(self):
        """ALDX: 'Aldeyra resubmitted the reproxalap NDA, which...'"""
        from pdufa_scraper import _parse_drug_and_action_near_pdufa
        text = (
            "Aldeyra resubmitted the reproxalap NDA, which, based on "
            "written agreement with the FDA, primarily consisted of "
            "results from the additional dry eye chamber trial. On July "
            "17, 2025, Aldeyra announced that the FDA accepted the "
            "reproxalap NDA for review and assigned a PDUFA date of "
            "December 16, 2025."
        )
        drug, action = _parse_drug_and_action_near_pdufa(text)
        assert "reproxalap" in drug.lower()

    def test_real_capr_seeking_approval_of_pattern(self):
        """CAPR: 'BLA seeking full approval of Deramiocel, an...'"""
        from pdufa_scraper import _parse_drug_and_action_near_pdufa
        text = (
            "lifted the previously issued CRL and resumed review of the "
            "Company's BLA seeking full approval of Deramiocel, an "
            "investigational cell therapy, for the treatment of DMD "
            "cardiomyopathy. The submission has been classified as a "
            "Class 2 resubmission, with a Prescription Drug User Fee Act "
            "(PDUFA) target action date of August 22, 2026."
        )
        drug, action = _parse_drug_and_action_near_pdufa(text)
        assert "deramiocel" in drug.lower()

    def test_real_achv_commercialization_of_pattern(self):
        """ACHV: 'commercialization of cytisinicline as a treatment of'"""
        from pdufa_scraper import _parse_drug_and_action_near_pdufa
        text = (
            "potential commercialization of cytisinicline as a treatment "
            "of nicotine dependence. In September 2025, the company "
            "announced that its New Drug Application, submitted to the "
            "U.S. Food and Drug Administration (FDA) in June 2025, had "
            "been accepted for review. The FDA has assigned a "
            "Prescription Drug User Fee Act (PDUFA) date of June 20, 2026."
        )
        drug, action = _parse_drug_and_action_near_pdufa(text)
        assert "cytisinicline" in drug.lower()

    def test_real_arvn_approval_of_with_brand_paren_generic(self):
        """ARVN: 'FDA Approval of VEPPANU (vepdegestrant) for the...'"""
        from pdufa_scraper import _parse_drug_and_action_near_pdufa
        text = (
            "Arvinas Announces FDA Approval of VEPPANU (vepdegestrant) "
            "for the Treatment of ESR1m, ER+/HER2- Advanced Breast Cancer. "
            "VEPPANU is the first-and-only FDA-approved PROTAC. Approval "
            "received in advance of FDA-assigned PDUFA date of June 5, 2026."
        )
        drug, action = _parse_drug_and_action_near_pdufa(text)
        # Either VEPPANU or vepdegestrant is acceptable
        assert ("veppanu" in drug.lower() or "vepdegestrant" in drug.lower())


class TestEdgarFetchIntegration:
    """Validates the end-to-end EDGAR path with mocked HTTP."""

    def test_fetch_from_edgar_with_mocked_response(self):
        from pdufa_scraper import fetch_pdufa_events_from_edgar
        fake_search = {
            "hits": {
                "hits": [
                    {
                        "_id": "0001104659-26-000001:exhibit99-1.htm",
                        "_source": {
                            "display_names": [
                                "Spero Therapeutics, Inc.  (SPRO)  (CIK 0001701108)"
                            ],
                            "ciks": ["0001701108"],
                            "adsh": "0001104659-26-000001",
                        },
                    }
                ]
            }
        }
        fake_filing_text = (
            "Spero Therapeutics announced that FDA accepted the NDA "
            "for tebipenem HBr with a PDUFA target action date of "
            "October 30, 2026."
        )
        with patch(
            "pdufa_scraper._fetch_edgar_search", return_value=fake_search
        ), patch(
            "pdufa_scraper._fetch_filing_text", return_value=fake_filing_text
        ):
            events = fetch_pdufa_events_from_edgar()
        assert len(events) == 1
        assert events[0]["ticker"] == "SPRO"
        assert events[0]["pdufa_date"] == "2026-10-30"
        assert events[0]["source"] == "edgar_8k"

    def test_skips_hits_with_no_ticker(self):
        from pdufa_scraper import fetch_pdufa_events_from_edgar
        fake_search = {
            "hits": {
                "hits": [
                    {
                        "_id": "0001104659-26-000001:doc.htm",
                        "_source": {
                            "display_names": ["Private Co (CIK 0001234567)"],
                            "ciks": ["0001234567"],
                            "adsh": "0001104659-26-000001",
                        },
                    }
                ]
            }
        }
        with patch(
            "pdufa_scraper._fetch_edgar_search", return_value=fake_search
        ):
            events = fetch_pdufa_events_from_edgar()
        assert events == []

    def test_dedupes_same_ticker_same_date(self):
        from pdufa_scraper import fetch_pdufa_events_from_edgar
        # Two filings from same ticker, both reciting the same PDUFA date.
        # _id format MUST be "<adsh>:<filename>" — that's how the fetcher
        # extracts the document filename.
        fake_search = {
            "hits": {
                "hits": [
                    {
                        "_id": "0001-A:doc1.htm",
                        "_source": {
                            "display_names": ["Co  (XYZ)  (CIK 0001)"],
                            "ciks": ["0001"],
                            "adsh": "0001-A",
                        },
                    },
                    {
                        "_id": "0001-B:doc2.htm",
                        "_source": {
                            "display_names": ["Co  (XYZ)  (CIK 0001)"],
                            "ciks": ["0001"],
                            "adsh": "0001-B",
                        },
                    },
                ]
            }
        }
        text = "PDUFA date of December 15, 2026 confirmed."
        with patch(
            "pdufa_scraper._fetch_edgar_search", return_value=fake_search
        ), patch(
            "pdufa_scraper._fetch_filing_text", return_value=text
        ):
            events = fetch_pdufa_events_from_edgar(polite_sleep_seconds=0)
        assert len(events) == 1
        assert events[0]["ticker"] == "XYZ"
        assert events[0]["pdufa_date"] == "2026-12-15"
