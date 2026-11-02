"""2026-11-02 — Tier-4 self-built alt-data (docs/25 D12): Reg SHO threshold
list, SEC fails-to-deliver, Form 144, IPO lock-up proxy, CBOE VIX term
structure, own-universe breadth. Parsers against the live file shapes
(captured 2026-11-02), fail-closed has_data contracts, the six rules,
and every integration point (aggregators, features, prompt, weights,
labels, model features, screener hook). No network.
"""
from __future__ import annotations

import io
import os
import sys
import time
import zipfile
from datetime import date

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

import altdata_tier4 as t4  # noqa: E402
import market_breadth as mb  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_caches(monkeypatch):
    monkeypatch.setattr(t4, "_CACHE", {})
    mb.reset()
    yield
    mb.reset()


NASDAQ_TXT = ("Symbol|Security Name|Market Category|Reg SHO Threshold Flag|Rule 3210|Filler\r\n"
              "AAPD|DIREXION SHS ETF TR DAILY AAPL|G|Y|N|\r\nAARD|AARDVARK THERAPEUTICS INC COM|Q|Y|N|\r\n"
              "NOPE|NOT FLAGGED|Q|N|N|\r\nFile Creation Time: 1030202620:15\r\n")
NYSE_TXT = ("Symbol|Security Name|Market Category|Reg SHO Threshold Flag|Filler|Filler\n"
            "BMNZ|Defiance Daily Target 2X Short BMNR ETF|NYSE Arca|Y||\nBURU|Nuburu, Inc.|NYSE American|Y||\n")
FTD_TXT = ("SETTLEMENT DATE|CUSIP|SYMBOL|QUANTITY (FAILS)|DESCRIPTION|PRICE\n"
           "20261001|037833100|AAPL|1000|APPLE INC;COM NPV|330.32\n"
           "20261002|037833100|AAPL|1200|APPLE INC;COM NPV|333.69\n"
           "20261006|037833100|AAPL|150000|APPLE INC;COM NPV|.\n"
           "20261007|037833100|AAPL|180000|APPLE INC;COM NPV|332.89\n"
           "20261007|B5950S113|MDXH|630|MDXHEALTH SA SHS NEW(BELGIUM) |0.58\n"
           "Trailer record count 5\nTrailer total quantity of shares 332830\n")
CBOE_CSV = "DATE,OPEN,HIGH,LOW,CLOSE\n10/30/2026,20.1,21.0,19.9,{close}\n"


def _zip_bytes(name, text):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, text)
    return buf.getvalue()


class TestRegSho:
    def test_parse_both_venue_shapes(self):
        assert t4.parse_threshold_file(NASDAQ_TXT) == {"AAPD", "AARD"}
        assert t4.parse_threshold_file(NYSE_TXT) == {"BMNZ", "BURU"}

    def test_lookup_unions_venues_and_fails_closed(self, monkeypatch):
        def fake_get(url, timeout=30):
            if "nasdaqtrader" in url and "20261030" in url:
                return NASDAQ_TXT
            if "nyse.com" in url and "2026-10-30" in url:
                return NYSE_TXT
            return None
        monkeypatch.setattr(t4, "_http_get_text", fake_get)
        monkeypatch.setattr(t4, "_business_days_back", lambda n, today=None: [date(2026, 10, 30)])
        r = t4.get_reg_sho_threshold("burU")
        assert r["has_data"] and r["on_threshold_list"] and r["venues"] == ["nyse"]
        assert t4.get_reg_sho_threshold("MSFT") == {
            "has_data": True, "on_threshold_list": False, "venues": [],
            "list_dates": {"nasdaq": "2026-10-30", "nyse": "2026-10-30"}, "list_size": 4}
        monkeypatch.setattr(t4, "_CACHE", {})
        monkeypatch.setattr(t4, "_http_get_text", lambda url, timeout=30: None)
        assert t4.get_reg_sho_threshold("AAPL")["has_data"] is False


class TestRegShoLiveShapes:
    """Shapes seen on 2026-11-02 at 11:50 ET: Nasdaq answers today's
    not-yet-published date with an HTML page (HTTP 200); NYSE answers
    with a header-only stub plus a timestamp trailer. Neither is a list."""
    HTML = "\n\n<!DOCTYPE html PUBLIC ...><html><body>Symbol lookup | not found</body></html>"
    STUB = "Symbol|Security Name|Market Category|Reg SHO Threshold Flag|Filler|Filler\n20261102210500\n"

    def test_html_and_header_only_are_not_lists(self):
        assert t4._looks_like_threshold_file(self.HTML) is False
        assert t4._looks_like_threshold_file(self.STUB) is True      # right shape…
        assert t4.parse_threshold_file(self.STUB) == set()           # …but empty → skipped by the loader
        assert t4._looks_like_threshold_file(NASDAQ_TXT) and t4._looks_like_threshold_file(NYSE_TXT)

    def test_loader_falls_back_to_the_previous_day(self, monkeypatch):
        def fake_get(url, timeout=30):
            if "20261102" in url:
                return self.HTML
            if "2026-11-02" in url:
                return self.STUB
            if "20261030" in url:
                return NASDAQ_TXT
            if "2026-10-30" in url:
                return NYSE_TXT
            return None
        monkeypatch.setattr(t4, "_http_get_text", fake_get)
        monkeypatch.setattr(t4, "_business_days_back",
                            lambda n, today=None: [date(2026, 11, 2), date(2026, 10, 30)])
        lists = t4.load_threshold_lists()
        assert lists["dates"] == {"nasdaq": "2026-10-30", "nyse": "2026-10-30"} and lists["count"] == 4


class TestFailsToDeliver:
    def test_parse_skips_header_trailer_and_dot_price(self):
        d = t4.parse_ftd_text(FTD_TXT)
        assert [q for _d, q, _p in d["AAPL"]] == [1000, 1200, 150000, 180000]
        assert d["AAPL"][2][2] is None and d["MDXH"][0][2] == 0.58

    def test_period_names_newest_first(self):
        assert t4._ftd_period_names(date(2026, 11, 2))[:3] == [
            "cnsfails202611a", "cnsfails202610b", "cnsfails202610a"]
        assert t4._ftd_period_names(date(2026, 11, 20))[0] == "cnsfails202611b"

    def test_loads_from_second_url_root_and_detects_rising(self, monkeypatch):
        calls = []
        def fake_bytes(url, timeout=30):
            calls.append(url)
            if "cnsfails202610a" in url and "/other/" in url:
                return _zip_bytes("cnsfails202610a.txt", FTD_TXT)
            return None            # everything else 404s (not yet published / old root)
        monkeypatch.setattr(t4, "_http_get_bytes", fake_bytes)
        monkeypatch.setattr(t4, "_ftd_period_names", lambda today=None: ["cnsfails202611a", "cnsfails202610b", "cnsfails202610a"])
        r = t4.get_fails_to_deliver("AAPL")
        assert r["has_data"] and r["reported"] and r["trend"] == "rising" and r["is_large"]
        assert r["latest_fails"] == 180000 and r["latest_date"] == "2026-10-07"
        assert any("/files/data/other/" in u for u in calls)
        none = t4.get_fails_to_deliver("ZZZZ")
        assert none["has_data"] and none["reported"] is False and none["latest_fails"] == 0

    def test_no_file_reachable_is_not_a_fact(self, monkeypatch):
        monkeypatch.setattr(t4, "_http_get_bytes", lambda url, timeout=30: None)
        assert t4.get_fails_to_deliver("AAPL")["has_data"] is False


class TestForm144:
    def test_counts_and_cluster(self, monkeypatch):
        import sec_filings
        today = date.today().isoformat()
        monkeypatch.setattr(sec_filings, "get_company_filings",
                            lambda sym, form_types=None, since_date=None: [
                                {"form_type": "144", "filed_date": today},     # the real key (sec_filings)
                                {"form": "144", "filing_date": today},
                                {"form": "144/A", "filing_date": "2020-01-01"}])
        r = t4.get_form144("AAPL")
        assert r["has_data"] and r["count_30d"] == 2 and r["count_90d"] == 3 and r["is_cluster_30d"]
        assert r["latest_days_ago"] == 0

    def test_edgar_failure_is_fail_closed(self, monkeypatch):
        import sec_filings
        def boom(*a, **k):
            raise OSError("edgar down")
        monkeypatch.setattr(sec_filings, "get_company_filings", boom)
        assert t4.get_form144("AAPL")["has_data"] is False


class TestIpoLockup:
    def test_window_arithmetic(self):
        today = date(2026, 11, 2)
        r = t4._lockup_from_first_bar(date(2026, 5, 10), 120, today)     # listed 176d → expiry in 4d
        assert r["is_recent_listing"] and r["days_to_lockup_expiry"] == 4 and r["in_lockup_window"]
        r2 = t4._lockup_from_first_bar(date(2026, 2, 1), 190, today)     # expiry 2026-07-31, long past
        assert r2["is_recent_listing"] and not r2["in_lockup_window"]
        old = t4._lockup_from_first_bar(date(2020, 1, 1), 600, today)
        assert old["is_recent_listing"] is False

    def test_reads_first_bar_from_bars(self, monkeypatch):
        import pandas as pd
        import market_data
        idx = pd.date_range("2026-05-11", periods=120, freq="B")
        df = pd.DataFrame({"close": range(120)}, index=idx)
        monkeypatch.setattr(market_data, "get_bars", lambda symbol, limit=600, **kw: df)
        r = t4.get_ipo_lockup("NEWCO")
        assert r["has_data"] and r["is_recent_listing"] and r["first_bar_date"] == "2026-05-11"
        monkeypatch.setattr(market_data, "get_bars", lambda symbol, limit=600, **kw: pd.DataFrame())
        assert t4.get_ipo_lockup("NEWCO")["has_data"] is False


class TestVixTerm:
    def test_parse_last_close(self):
        assert t4.parse_cboe_last_close(CBOE_CSV.format(close=22.5)) == ("2026-10-30", 22.5)
        assert t4.parse_cboe_last_close("DATE,OPEN,HIGH,LOW,CLOSE\n") is None

    def test_backwardation_and_contango(self, monkeypatch):
        closes = {"VIX9D": 30.0, "VIX": 28.0, "VIX3M": 24.0, "VIX6M": 23.0}
        monkeypatch.setattr(t4, "_http_get_text",
                            lambda url, timeout=30: next(CBOE_CSV.format(close=v) for k, v in closes.items()
                                                         if f"/{k}_History" in url))
        r = t4.get_vix_term_structure()
        assert r["has_data"] and r["structure"] == "backwardation" and r["stress"]
        assert r["ratio_1m_3m"] == pytest.approx(28 / 24, abs=1e-3)
        monkeypatch.setattr(t4, "_CACHE", {})
        closes.update({"VIX9D": 14.0, "VIX": 15.0, "VIX3M": 18.0})
        r2 = t4.get_vix_term_structure()
        assert r2["structure"] == "contango" and not r2["stress"]

    def test_missing_series_is_fail_closed(self, monkeypatch):
        monkeypatch.setattr(t4, "_http_get_text", lambda url, timeout=30: None)
        assert t4.get_vix_term_structure()["has_data"] is False


class TestBreadth:
    def _df(self, closes):
        import pandas as pd
        return pd.DataFrame({"close": closes})

    def test_thin_sample_is_not_breadth(self):
        for i in range(10):
            mb.record_bars(f"S{i}", self._df([1, 2]))
        assert mb.snapshot()["has_data"] is False

    def test_percentages_and_regime(self):
        for i in range(50):
            up = i < 40
            # last close 13 vs a 20-day mean just under it (up) / 7 vs 8+ (down)
            closes = ([10.0] * 25) + ([12.0] * 24 + [13.0] if up else [8.0] * 24 + [7.0])
            mb.record_bars(f"S{i}", self._df(closes))
        s = mb.snapshot()
        assert s["has_data"] and s["n"] == 50
        assert s["pct_above_sma20"] == 80.0 and s["pct_above_sma50"] == 80.0
        assert s["regime"] == "thrust" and s["advancers"] == 40 and s["decliners"] == 10
        assert s["adv_dec"] == 4.0
        mb.reset()
        for i in range(50):
            mb.record_bars(f"S{i}", self._df([10.0] * 25 + [8.0] * 24 + [7.0]))
        assert mb.snapshot()["regime"] == "washout"

    def test_stale_records_age_out(self):
        for i in range(50):
            mb.record_bars(f"S{i}", self._df([1.0, 2.0]))
        assert mb.snapshot(now=time.time() + mb.RECENT_SEC + 1)["has_data"] is False


class TestRules:
    def _c(self, signal, alt):
        return {"symbol": "X", "signal": signal, "alt_data": alt}

    def test_each_rule_fires_and_no_ops(self):
        import importlib
        cases = [
            ("reg_sho_threshold_short_caution", "SHORT",
             {"reg_sho_threshold": {"has_data": True, "on_threshold_list": True, "venues": ["nyse"]}}),
            ("ftd_rising_short_caution", "SELL",
             {"fails_to_deliver": {"has_data": True, "reported": True, "trend": "rising", "is_large": True,
                                   "latest_fails": 180000, "latest_date": "2026-10-07", "max_fails": 180000}}),
            ("form144_cluster_long_caution", "BUY",
             {"form144": {"has_data": True, "is_cluster_30d": True, "count_30d": 3, "latest_days_ago": 2}}),
            ("ipo_lockup_expiry_long_caution", "BUY",
             {"ipo_lockup": {"has_data": True, "is_recent_listing": True, "in_lockup_window": True,
                             "days_to_lockup_expiry": 4, "lockup_expiry_est": "2026-11-06", "days_listed": 176}}),
            ("vix_backwardation_caution", "BUY",
             {"macro": {"vix_term": {"has_data": True, "structure": "backwardation", "vix": 28.0,
                                     "vix3m": 24.0, "ratio_1m_3m": 1.17}}}),
            ("breadth_washout_long_caution", "BUY",
             {"macro": {"breadth": {"has_data": True, "regime": "washout", "pct_above_sma20": 22.0,
                                    "n": 300, "adv_dec": 0.4}}}),
        ]
        for name, sig, alt in cases:
            mod = importlib.import_module(f"deterministic_specialists.{name}")
            v = mod.evaluate(self._c(sig, alt), None)
            assert v and v["severity"] == "CAUTION", name
            assert mod.evaluate({"symbol": "X", "signal": sig}, None) is None, name
            # has_data False never fires
            dead = {k: ({**v2, "has_data": False} if k != "macro" else
                        {mk: {**mv, "has_data": False} for mk, mv in v2.items()}) for k, v2 in alt.items()}
            assert mod.evaluate(self._c(sig, dead), None) is None, name

    def test_rules_registered(self):
        from deterministic_specialists import RULE_MODULES, discover_rules
        for n in ("reg_sho_threshold_short_caution", "ftd_rising_short_caution",
                  "form144_cluster_long_caution", "ipo_lockup_expiry_long_caution",
                  "vix_backwardation_caution", "breadth_washout_long_caution"):
            assert f"deterministic_specialists.{n}" in RULE_MODULES
        assert len(discover_rules()) == len(RULE_MODULES)


class TestIntegration:
    def test_aggregators_carry_the_new_keys(self):
        src = open(os.path.join(REPO, "alternative_data.py")).read()
        for k in ('"reg_sho_threshold"', '"fails_to_deliver"', '"form144"', '"ipo_lockup"'):
            assert k in src
        m = open(os.path.join(REPO, "macro_data.py")).read()
        assert '"vix_term"' in m and '"breadth"' in m

    def test_features_prompt_weights_labels_model(self):
        tp = open(os.path.join(REPO, "trade_pipeline.py")).read()
        for k in ("reg_sho_threshold", "ftd_latest", "form144_count_30d", "ipo_lockup_window",
                  "_vix_term_ratio", "_breadth_pct_above_sma20"):
            assert f'features_payload["{k}"]' in tp, k
        aa = open(os.path.join(REPO, "ai_analyst.py")).read()
        assert "REG SHO THRESHOLD LIST" in aa and "VIX TERM STRUCTURE" in aa and "BREADTH (" in aa
        from signal_weights import signal_names
        assert {"reg_sho_threshold", "ftd_latest", "form144_count_30d", "ipo_lockup_window"} <= signal_names()
        from display_names import humanize
        assert humanize("reg_sho_threshold") == "Reg SHO Threshold List"
        import meta_model
        feats = [n for n in dir(meta_model) if n.isupper() and "FEATURE" in n]
        src = open(os.path.join(REPO, "meta_model.py")).read()
        assert '"_breadth_pct_above_sma20"' in src and feats

    def test_screener_records_breadth(self):
        src = open(os.path.join(REPO, "screener.py")).read()
        assert "from market_breadth import record_bars" in src
