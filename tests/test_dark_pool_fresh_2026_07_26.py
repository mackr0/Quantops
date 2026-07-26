"""Dark pool reconnect (P1.2, 2026-07-26).

The original FINRA ATS reader had no date filter and FINRA's default
ordering served week 2023-11-06 on 86% of payloads — three-year-old
rows presented as current. It also summed EVERY returned row: the
per-symbol aggregate (ATS_W_SMBL), the per-firm breakdown of the SAME
volume (ATS_W_SMBL_FIRM — a double count), and non-ATS wholesaler OTC
rows. Separately, the pipeline's `dark_pool_pct` feature read a key
the reader never returned (`ats_pct_of_total`) — a constant 0.

Pinned here: the dated query window, client-side freshness guard,
correct row taxonomy, the real ats_pct_of_total (vs consolidated
volume, refusing >100% artifacts), and the pipeline/prompt wiring.
"""
from __future__ import annotations

import io
import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

import alternative_data as ad  # noqa: E402


def _row(week, code, shares, trades=10, mpid=None):
    return {"weekStartDate": week, "summaryTypeCode": code,
            "totalWeeklyShareQuantity": shares,
            "totalWeeklyTradeCount": trades, "MPID": mpid}


def _run_reader(payload_rows, symbol="AAPL", pct=17.8):
    """Drive get_dark_pool_volume with a canned FINRA response and a
    stubbed consolidated-volume denominator. Returns (result, body)."""
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data)
        return io.BytesIO(json.dumps(payload_rows).encode())

    with patch.object(ad, "_get_cached", return_value=None), \
         patch.object(ad, "_set_cached"), \
         patch.object(ad, "urlopen", side_effect=fake_urlopen), \
         patch.object(ad, "_ats_pct_of_consolidated", return_value=pct):
        out = ad.get_dark_pool_volume(symbol)
    return out, captured["body"]


class TestQueryWindow:
    def test_request_carries_date_range_filter(self):
        _, body = _run_reader([])
        drf = body.get("dateRangeFilters")
        assert drf and drf[0]["fieldName"] == "weekStartDate", (
            "the FINRA query lost its dateRangeFilters — FINRA's "
            "default ordering serves 2023 rows as current again."
        )
        from datetime import date, timedelta
        start = date.fromisoformat(drf[0]["startDate"])
        assert (date.today() - start).days <= ad._DARK_POOL_MAX_AGE_DAYS

    def test_stale_rows_in_response_are_refused(self):
        """Server-side surprise: response contains ONLY old rows."""
        out, _ = _run_reader([
            _row("2023-11-06", "ATS_W_SMBL", 9_999_999),
            _row("2023-11-06", "ATS_W_SMBL_FIRM", 5_000_000, mpid="X"),
        ])
        assert out["ats_volume"] == 0 and out["week_start"] is None, (
            "rows older than the freshness window were served as "
            "current — the 2023-11-06 incident class."
        )


class TestRowTaxonomy:
    def _fresh_week(self):
        from datetime import date, timedelta
        return (date.today() - timedelta(days=10)).isoformat()

    def test_aggregate_only_no_double_count(self):
        wk = self._fresh_week()
        from datetime import date, timedelta
        older = (date.today() - timedelta(days=17)).isoformat()
        out, _ = _run_reader([
            # latest week: per-symbol aggregate + firm breakdown + OTC
            _row(wk, "ATS_W_SMBL", 1_000_000, trades=500),
            _row(wk, "ATS_W_SMBL_FIRM", 600_000, mpid="UBSA"),
            _row(wk, "ATS_W_SMBL_FIRM", 400_000, mpid="MSPL"),
            _row(wk, "OTC_W_SMBL", 3_000_000, trades=900),
            _row(wk, "OTC_W_SMBL_FIRM", 3_000_000, mpid="CDRG"),
            # an older (still-fresh) week must be ignored entirely
            _row(older, "ATS_W_SMBL", 7_777_777),
        ])
        assert out["ats_volume"] == 1_000_000, (
            "volume must come from the latest week's ATS_W_SMBL "
            "aggregate only — firm rows double it, OTC rows aren't "
            "dark pools, older weeks aren't current."
        )
        assert out["ats_trade_count"] == 500
        assert out["num_venues"] == 2
        assert out["week_start"] == wk
        assert out["ats_pct_of_total"] == 17.8

    def test_empty_response_shape(self):
        out, _ = _run_reader([])
        assert out == {"ats_volume": 0, "ats_trade_count": 0,
                       "num_venues": 0, "week_start": None,
                       "ats_pct_of_total": None}


class TestPctDenominator:
    def test_refuses_over_100_pct_artifact(self):
        class _H:
            def history(self, start=None, end=None):
                import pandas as pd
                return pd.DataFrame({"Volume": [100.0]})
        with patch.object(ad.yf, "Ticker", return_value=_H()):
            assert ad._ats_pct_of_consolidated(
                "AAPL", "2026-06-29", 1_000) is None

    def test_normal_pct(self):
        class _H:
            def history(self, start=None, end=None):
                import pandas as pd
                return pd.DataFrame({"Volume": [50_000.0, 50_000.0]})
        with patch.object(ad.yf, "Ticker", return_value=_H()):
            assert ad._ats_pct_of_consolidated(
                "AAPL", "2026-06-29", 20_000) == 20.0

    def test_denominator_failure_returns_none(self):
        class _H:
            def history(self, start=None, end=None):
                raise RuntimeError("yf down")
        with patch.object(ad.yf, "Ticker", return_value=_H()):
            assert ad._ats_pct_of_consolidated(
                "AAPL", "2026-06-29", 20_000) is None


class TestWiring:
    def test_pipeline_reads_existing_key_null_safe(self):
        src = open(os.path.join(REPO, "trade_pipeline.py")).read()
        assert ('features_payload["dark_pool_pct"] = '
                'alt.get("dark_pool", {}).get("ats_pct_of_total") or 0'
                ) in src, (
            "the dark_pool_pct feature no longer reads the reader's "
            "ats_pct_of_total key null-safely — either the constant-0 "
            "bug or a None leak is back."
        )

    def test_reader_returns_the_key_the_pipeline_reads(self):
        out, _ = _run_reader([])
        assert "ats_pct_of_total" in out

    def test_prompt_renders_pct_and_week(self):
        src = open(os.path.join(REPO, "ai_analyst.py")).read()
        assert "ats_pct_of_total" in src and "week_start" in src, (
            "the AI prompt's dark-pool line dropped the pct/week "
            "context."
        )
