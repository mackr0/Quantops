"""Tests for sec_filings.py — Phase 4 of Quant Fund Evolution.

Covers:
  - Section extraction from synthetic filing text
  - Going concern and material weakness flag detection
  - Severity-gated alert retrieval
  - Filing row persistence (save/load round-trip)
  - Decision helpers that don't require network

Network-dependent functions (CIK lookup, filing fetch, AI diff) are not
tested here — they're covered by the Phase 4 deployment verification step.
"""

import json
import sqlite3

import pytest


# ---------------------------------------------------------------------------
# Fixtures: synthetic filing text and DB
# ---------------------------------------------------------------------------

RISK_FACTORS_SAMPLE = """
ITEM 1A. RISK FACTORS

Investing in our common stock involves a high degree of risk. You should
carefully consider the following risks, together with all of the other
information in this report.

Risks Related to Our Business

Our business depends on continued growth in the cloud computing market.

We have a limited operating history and have incurred losses every year.

There is substantial doubt about our ability to continue as a going concern.

ITEM 1B. UNRESOLVED STAFF COMMENTS

None.
"""

MDNA_SAMPLE = """
ITEM 7. MANAGEMENT'S DISCUSSION AND ANALYSIS

We experienced significant revenue growth this year, but costs rose faster
than expected. Our internal controls over financial reporting have a
material weakness in internal control related to revenue recognition.

ITEM 7A. QUANTITATIVE AND QUALITATIVE DISCLOSURES

Not applicable.
"""

FULL_FILING = RISK_FACTORS_SAMPLE + "\n" + MDNA_SAMPLE


@pytest.fixture
def tmp_filings_db(tmp_path):
    """Create a profile DB with the sec_filings_history table."""
    db = str(tmp_path / "test_profile.db")
    from journal import init_db
    init_db(db)
    return db


# ---------------------------------------------------------------------------
# Section extraction
# ---------------------------------------------------------------------------

class TestSectionExtraction:
    def test_extract_risk_factors(self):
        from sec_filings import extract_sections
        result = extract_sections(FULL_FILING)
        assert result["risk_factors"] is not None
        assert "going concern" in result["risk_factors"].lower()
        assert "UNRESOLVED STAFF COMMENTS" not in result["risk_factors"]

    def test_extract_mdna(self):
        from sec_filings import extract_sections
        result = extract_sections(FULL_FILING)
        assert result["mdna"] is not None
        assert "material weakness" in result["mdna"].lower()

    def test_going_concern_flag(self):
        from sec_filings import extract_sections
        result = extract_sections(FULL_FILING)
        assert result["going_concern_flag"] is True

    def test_no_going_concern_when_absent(self):
        from sec_filings import extract_sections
        clean_filing = "ITEM 1A. RISK FACTORS\n\nOur business is strong.\n\nITEM 2. PROPERTIES"
        result = extract_sections(clean_filing)
        assert result["going_concern_flag"] is False

    def test_material_weakness_flag(self):
        from sec_filings import extract_sections
        result = extract_sections(FULL_FILING)
        assert result["material_weakness_flag"] is True

    def test_empty_text_returns_none_sections(self):
        from sec_filings import extract_sections
        result = extract_sections("")
        assert result["risk_factors"] is None
        assert result["mdna"] is None
        assert result["going_concern_flag"] is False
        assert result["material_weakness_flag"] is False


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_save_and_retrieve_filing(self, tmp_filings_db):
        from sec_filings import save_filing_row, get_latest_filing_in_db

        row = {
            "symbol": "AAPL",
            "accession_number": "0000320193-24-000001",
            "form_type": "10-K",
            "filed_date": "2024-01-15",
            "filing_url": "http://example/aapl.htm",
            "risk_factors_text": "Risk factors section text.",
            "mdna_text": "MDNA section text.",
            "going_concern_flag": False,
            "material_weakness_flag": False,
            "analyzed_at": "2024-01-16T00:00:00",
            "alert_severity": "low",
            "alert_signal": "neutral",
            "alert_summary": "No material changes.",
            "alert_changes_json": "[]",
        }
        save_filing_row(tmp_filings_db, row)

        latest = get_latest_filing_in_db(tmp_filings_db, "AAPL", "10-K")
        assert latest is not None
        assert latest["symbol"] == "AAPL"
        assert latest["form_type"] == "10-K"
        assert latest["alert_severity"] == "low"

    def test_upsert_replaces_same_accession(self, tmp_filings_db):
        from sec_filings import save_filing_row

        row = {
            "symbol": "MSFT",
            "accession_number": "0000000001",
            "form_type": "10-K",
            "filed_date": "2024-01-01",
            "filing_url": "",
            "risk_factors_text": "v1",
            "alert_severity": "low",
            "alert_signal": "neutral",
            "alert_summary": "",
            "alert_changes_json": "[]",
        }
        save_filing_row(tmp_filings_db, row)
        row["alert_severity"] = "high"
        row["alert_summary"] = "updated"
        save_filing_row(tmp_filings_db, row)

        conn = sqlite3.connect(tmp_filings_db)
        rows = conn.execute(
            "SELECT * FROM sec_filings_history WHERE symbol=?", ("MSFT",)
        ).fetchall()
        conn.close()
        assert len(rows) == 1

    def test_no_latest_when_empty(self, tmp_filings_db):
        from sec_filings import get_latest_filing_in_db
        assert get_latest_filing_in_db(tmp_filings_db, "NONE", "10-K") is None


# ---------------------------------------------------------------------------
# Active alert retrieval
# ---------------------------------------------------------------------------

class TestActiveAlerts:
    def _populate(self, db, entries):
        from sec_filings import save_filing_row
        for i, (sym, form, date, severity) in enumerate(entries):
            save_filing_row(db, {
                "symbol": sym,
                "accession_number": f"acc-{i}",
                "form_type": form,
                "filed_date": date,
                "filing_url": "",
                "risk_factors_text": "",
                "analyzed_at": "2024-01-01T00:00:00",
                "alert_severity": severity,
                "alert_signal": "neutral",
                "alert_summary": f"Summary {i}",
                "alert_changes_json": "[]",
            })

    def test_filters_by_severity(self, tmp_filings_db):
        from datetime import datetime, timedelta
        from sec_filings import get_active_alerts

        recent_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        self._populate(tmp_filings_db, [
            ("A", "10-K", recent_date, "low"),
            ("B", "10-Q", recent_date, "medium"),
            ("C", "8-K", recent_date, "high"),
        ])

        all_alerts = get_active_alerts(tmp_filings_db, min_severity="low")
        assert len(all_alerts) == 3

        medium_up = get_active_alerts(tmp_filings_db, min_severity="medium")
        assert len(medium_up) == 2
        assert {a["symbol"] for a in medium_up} == {"B", "C"}

        high_only = get_active_alerts(tmp_filings_db, min_severity="high")
        assert len(high_only) == 1

    def test_filters_by_symbols(self, tmp_filings_db):
        from datetime import datetime, timedelta
        from sec_filings import get_active_alerts

        recent = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
        self._populate(tmp_filings_db, [
            ("AAPL", "10-K", recent, "high"),
            ("MSFT", "10-Q", recent, "high"),
        ])

        aapl_only = get_active_alerts(tmp_filings_db, symbols=["AAPL"], min_severity="low")
        assert len(aapl_only) == 1
        assert aapl_only[0]["symbol"] == "AAPL"

    def test_old_alerts_excluded(self, tmp_filings_db):
        from datetime import datetime, timedelta
        from sec_filings import get_active_alerts

        old = (datetime.now() - timedelta(days=120)).strftime("%Y-%m-%d")
        self._populate(tmp_filings_db, [
            ("OLD", "10-K", old, "high"),
        ])
        alerts = get_active_alerts(tmp_filings_db, min_severity="low")
        assert len(alerts) == 0


# ---------------------------------------------------------------------------
# Pipeline integration
# ---------------------------------------------------------------------------

class TestPipelineIntegration:
    def test_sec_alert_in_ai_prompt(self):
        """Verify the batch prompt includes SEC alert text when present."""
        from ai_analyst import _build_batch_prompt

        candidates = [{
            "symbol": "RISKY",
            "price": 10.0,
            "signal": "BUY",
            "score": 2,
            "rsi": 45,
            "volume_ratio": 1.5,
            "adx": 25,
            "stoch_rsi": 40,
            "roc_10": 2.0,
            "pct_from_52w_high": -15,
            "reason": "test",
            "sec_alert": {
                "form": "10-Q",
                "date": "2024-04-01",
                "severity": "high",
                "signal": "concerning",
                "summary": "New going concern language added to risk factors.",
            },
        }]
        portfolio = {"equity": 10000, "cash": 5000, "positions": [],
                     "num_positions": 0, "drawdown_pct": 0,
                     "drawdown_action": "normal"}
        market = {"regime": "bull", "vix": 18, "spy_trend": "up"}
        prompt = _build_batch_prompt(candidates, portfolio, market)

        assert "SEC ALERT" in prompt
        assert "HIGH" in prompt
        assert "going concern" in prompt.lower()
