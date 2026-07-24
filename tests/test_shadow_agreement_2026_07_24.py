"""Shadow eval must actually EVALUATE: schema-aware agreement, honest
digest taxonomy, and the quota stand-down.

2026-07-24, operator: "it seems i am paying money for nothing but
errors." Three defects behind that impression:
1. `_extract_signal` knew none of the house schemas (ensemble
   `verdict`, transcript `tone`, batch_select `trades`), so agreement
   was None on 100% of 543 successful shadow calls ever made — the
   A/B comparison the money buys never ran.
2. The digest's single "Errors" bucket conflated deliberate cost-cap
   throttling and account-billing quota deaths with real errors.
3. A billing-dead provider (OpenAI: "exceeded your current quota" on
   1,229/1,229 calls over two days) was still called ~280x/day.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shadow_eval import (  # noqa: E402
    _extract_signal, _compute_agreement, _quota_standdown,
    _STANDDOWN_MEMO,
)


class TestSchemaAwareSignal:
    def test_ensemble_verdict(self):
        assert _extract_signal({"verdict": "veto", "confidence": 85}) == "VETO"

    def test_transcript_tone(self):
        assert _extract_signal({"tone": "neutral", "key_phrases": []}) == "NEUTRAL"

    def test_batch_select_trades_canonical_set(self):
        a = {"trades": [{"symbol": "nvda", "action": "BUY"},
                        {"symbol": "COST", "action": "sell"}]}
        b = {"trades": [{"symbol": "COST", "action": "SELL"},
                        {"symbol": "NVDA", "action": "buy"}]}
        # order-insensitive, case-normalised
        assert _extract_signal(a) == _extract_signal(b) == "COST:SELL,NVDA:BUY"

    def test_batch_select_empty_is_pass(self):
        assert _extract_signal({"trades": [],
                                "portfolio_reasoning": "x"}) == "PASS"

    def test_legacy_keys_still_work(self):
        assert _extract_signal({"action": "buy"}) == "BUY"
        assert _extract_signal({"signal": "HOLD"}) == "HOLD"

    def test_unrecognisable_returns_none(self):
        assert _extract_signal({"foo": "bar"}) is None
        assert _extract_signal("not a dict") is None
        assert _extract_signal(None) is None


class TestAgreement:
    def test_verdict_match_and_mismatch(self):
        assert _compute_agreement({"verdict": "SELL"}, {"verdict": "sell"}) == 1
        assert _compute_agreement({"verdict": "SELL"}, {"verdict": "HOLD"}) == 0

    def test_trade_sets_agree_order_insensitive(self):
        p = {"trades": [{"symbol": "A", "action": "BUY"},
                        {"symbol": "B", "action": "SHORT"}]}
        s = {"trades": [{"symbol": "B", "action": "SHORT"},
                        {"symbol": "A", "action": "BUY"}]}
        assert _compute_agreement(p, s) == 1

    def test_ungradable_is_none(self):
        assert _compute_agreement({"verdict": "SELL"}, {"junk": 1}) is None


def _mk_shadow_db(path, errors):
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE ai_shadow_calls ("
        " id INTEGER PRIMARY KEY,"
        " timestamp TEXT NOT NULL DEFAULT (datetime('now')),"
        " provider TEXT, model TEXT, cost_usd REAL, error TEXT)")
    for e in errors:
        conn.execute(
            "INSERT INTO ai_shadow_calls (provider, model, error) "
            "VALUES ('openai', 'nano', ?)", (e,))
    conn.commit(); conn.close()
    return str(path)


QUOTA = ("RateLimitError: Error code: 429 - exceeded your current "
         "quota, please check your plan and billing details")


class TestQuotaStanddown:
    def setup_method(self):
        _STANDDOWN_MEMO.clear()

    def test_three_consecutive_quota_errors_stand_down(self, tmp_path):
        db = _mk_shadow_db(tmp_path / "q.db", [QUOTA, QUOTA, QUOTA])
        assert _quota_standdown(db, "openai") is True

    def test_transient_rate_limit_never_triggers(self, tmp_path):
        """An Anthropic-style transient 429 (no quota marker) must NOT
        stand the provider down — it self-heals."""
        db = _mk_shadow_db(tmp_path / "t.db",
                           ["429 rate limit exceeded"] * 3)
        assert _quota_standdown(db, "openai") is False

    def test_recent_success_resets(self, tmp_path):
        db = _mk_shadow_db(tmp_path / "s.db", [QUOTA, QUOTA, None])
        assert _quota_standdown(db, "openai") is False

    def test_fewer_than_three_keeps_probing(self, tmp_path):
        db = _mk_shadow_db(tmp_path / "f.db", [QUOTA, QUOTA])
        assert _quota_standdown(db, "openai") is False

    def test_cap_rows_ignored_in_the_window(self, tmp_path):
        """Cost-cap throttle rows must not mask the quota signature."""
        db = _mk_shadow_db(tmp_path / "c.db",
                           [QUOTA, QUOTA, QUOTA,
                            "shadow daily cost cap reached (est $0.01)"])
        assert _quota_standdown(db, "openai") is True


class TestDigestTaxonomy:
    def test_digest_separates_throttled_quota_and_errors(self):
        """Structural: the digest aggregation must bucket cost-cap and
        quota separately from real errors, and the table must carry the
        two new columns."""
        src = open(os.path.join(
            os.path.dirname(__file__), os.pardir,
            "notifications.py")).read()
        assert '"throttled"' in src and '"quota"' in src, (
            "The shadow digest lost its throttled/quota buckets — "
            "cost-cap and billing failures will again read as errors."
        )
        assert "Quota (billing)" in src and "Throttled (cap)" in src, (
            "The shadow digest table lost its taxonomy columns."
        )
