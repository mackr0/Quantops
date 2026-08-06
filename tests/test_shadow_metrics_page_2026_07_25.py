"""The /shadow page's decision-quality engine (shadow_metrics).

Operator directive 2026-07-25: the page showed nothing (it read the
retired `pipeline_shadow_runs` table) and must instead surface "all the
metrics that make it easy to determine which models are making the
right decisions more often and if there are different categories of
decisions that one model is better at."

Pins the grading semantics that make the numbers honest:
  - `actual_return_pct` is the RAW price return (verified live:
    SHORT predictions show positive pct on a rising price), so a
    bullish stance is right iff return > 0, bearish iff < 0.
  - Disagreement outcomes only count when a resolved prediction for
    the SAME symbol exists within shadow_metrics.MATCH_WINDOW_SEC of
    the shadow call (300s until 2026-07-30, 1800s after).
  - Cost-cap throttles and billing-quota deaths are never "errors".

2026-07-30: the neutral-stance and flat-outcome cases moved from
"ungradable" to graded — see test_shadow_grading_fairness_2026_07_30
for why. The assertions here were updated to the new semantics rather
than pinned to the old ones.
"""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shadow_metrics import (  # noqa: E402
    stance, grade, collect_fleet_metrics,
)


class TestStance:
    def test_classes(self):
        assert stance("BUY") == "bullish"
        assert stance("strong_sell") == "bearish"
        assert stance("VETO") == "bearish"
        assert stance("HOLD") == "neutral"
        assert stance("PASS") == "neutral"
        assert stance("COST:SELL,NVDA:BUY") is None   # apex set string
        assert stance(None) is None


class TestGrade:
    def test_raw_return_semantics(self):
        assert grade("bullish", 3.2) is True
        assert grade("bullish", -2.0) is False
        assert grade("bearish", -2.0) is True
        assert grade("bearish", 3.2) is False

    def test_ungradable_only_for_unmappable_signals(self):
        # None means "there is no stance to grade" — nothing else.
        assert grade(None, 5.0) is None          # apex set string
        assert grade("bullish", None) is None    # no outcome yet

    def test_neutral_and_flat_are_graded(self):
        # A HOLD is a real call and is scored like one.
        assert grade("neutral", 0.4) is True     # correctly sat out
        assert grade("neutral", 5.0) is False    # missed a real move
        assert grade("bullish", 0.4) is False    # bet on a non-move


def _mk_profile(tmp_path, name, shadow_rows, predictions=()):
    db = str(tmp_path / f"quantopsai_profile_{name}.db")
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE ai_shadow_calls (
            id INTEGER PRIMARY KEY, timestamp TEXT, purpose TEXT,
            provider TEXT, model TEXT, parsed_signal TEXT,
            agreement INTEGER, error TEXT, cost_usd REAL,
            latency_ms INTEGER, primary_parsed TEXT
        )""")
    conn.execute("""
        CREATE TABLE ai_predictions (
            id INTEGER PRIMARY KEY, symbol TEXT, timestamp TEXT,
            status TEXT, actual_return_pct REAL, predicted_signal TEXT
        )""")
    for r in shadow_rows:
        conn.execute(
            "INSERT INTO ai_shadow_calls (timestamp, purpose, provider,"
            " model, parsed_signal, agreement, error, cost_usd,"
            " latency_ms, primary_parsed) VALUES (?,?,?,?,?,?,?,?,?,?)",
            r)
    for p in predictions:
        # 3-tuple (sym, ts, ret) = legacy resolved row; 5-tuple adds
        # explicit (status, predicted_signal) for own-decision tests.
        if len(p) == 3:
            sym, ts, ret = p
            status, psig = "resolved", None
        else:
            sym, ts, ret, status, psig = p
        conn.execute(
            "INSERT INTO ai_predictions (symbol, timestamp, status,"
            " actual_return_pct, predicted_signal) VALUES (?,?,?,?,?)",
            (sym, ts, status, ret, psig))
    conn.commit()
    conn.close()
    return db


NOW = "2026-07-25 14:00:00"
PP_SELL = '{"symbol": "NVDA", "verdict": "SELL"}'


class TestCollect:
    def test_disagreement_outcome_grading(self, tmp_path):
        """Primary said SELL, shadow said BUY, price ROSE +4% →
        shadow right, primary wrong — the page's money metric.

        Uses a FORECAST specialist: since 2026-07-30 the VETO-authority
        specialists (risk_assessor, adversarial_reviewer) are graded as
        trade gates against a taken position's P&L, not as directional
        calls against raw price.
        """
        db = _mk_profile(
            tmp_path, "301",
            shadow_rows=[
                (NOW, "ensemble:pattern_recognizer", "anthropic", "haiku",
                 "BUY", 0, None, 0.001, 900, PP_SELL),
            ],
            predictions=[("NVDA", "2026-07-25T13:58:30", 4.0)],
        )
        m = collect_fleet_metrics([db])
        v = m["per_model"]["anthropic:haiku"]
        assert v["disagree"] == 1
        assert v["dis_resolved"] == 1
        assert v["shadow_right"] == 1
        assert v["primary_right"] == 0
        r = m["recent_disagreements"][0]
        assert r["who_right"] == "shadow"
        assert r["outcome_pct"] == 4.0

    def test_no_prediction_within_window_stays_pending(self, tmp_path):
        db = _mk_profile(
            tmp_path, "302",
            shadow_rows=[
                (NOW, "ensemble:risk_assessor", "anthropic", "haiku",
                 "BUY", 0, None, 0.001, 900, PP_SELL),
            ],
            predictions=[("NVDA", "2026-07-25T10:00:00", 4.0)],  # 4h off
        )
        m = collect_fleet_metrics([db])
        v = m["per_model"]["anthropic:haiku"]
        assert v["dis_resolved"] == 0 and v["dis_pending"] == 1

    def test_error_taxonomy_not_conflated(self, tmp_path):
        db = _mk_profile(
            tmp_path, "303",
            shadow_rows=[
                (NOW, "p", "openai", "nano", None, None,
                 "RateLimitError: exceeded your current quota",
                 0, 0, None),
                (NOW, "p", "openai", "nano", None, None,
                 "shadow daily cost cap reached (est $0.01)", 0, 0, None),
                (NOW, "p", "openai", "nano", None, None,
                 "ValueError: boom", 0, 0, None),
            ],
        )
        m = collect_fleet_metrics([db])
        v = m["per_model"]["openai:nano"]
        assert (v["quota"], v["throttled"], v["errors"]) == (1, 1, 1)

    def test_category_cuts_present(self, tmp_path):
        db = _mk_profile(
            tmp_path, "304",
            shadow_rows=[
                (NOW, "ensemble:risk_assessor", "anthropic", "haiku",
                 "SELL", 1, None, 0.001, 900, PP_SELL),
                (NOW, "transcript_sentiment", "anthropic", "haiku",
                 "NEUTRAL", 1, None, 0.001, 900,
                 '{"symbol": "AAPL", "tone": "neutral"}'),
            ],
        )
        m = collect_fleet_metrics([db])
        assert set(m["by_purpose"]) == {"ensemble:risk_assessor",
                                        "transcript_sentiment"}
        assert "bearish" in m["by_primary_action"]
        assert m["per_model"]["anthropic:haiku"]["agreement_pct"] == 100.0
        assert m["daily"]["2026-07-25"]["anthropic:haiku"]["graded"] == 2


class TestRouteWiring:
    def test_route_uses_the_metrics_engine(self):
        src = open(os.path.join(
            os.path.dirname(__file__), os.pardir, "views.py")).read()
        idx = src.index("def shadow_page")
        body = src[idx:idx + 1600]
        assert "collect_fleet_metrics" in body, (
            "/shadow no longer renders the model shadow-eval metrics — "
            "the page went dark once before by reading a retired table."
        )
        # the docstring may NAME the retired table; QUERYING it is
        # what must never come back
        assert "FROM pipeline_shadow_runs" not in body

    def test_template_carries_the_decision_sections(self):
        tpl = open(os.path.join(
            os.path.dirname(__file__), os.pardir,
            "templates", "shadow.html")).read()
        for needle in ("Per-model summary", "By decision category",
                       "By primary action", "Recent disagreements",
                       "Shadow won", "Units", "Daily agreement trend"):
            assert needle in tpl, f"template lost section: {needle}"


class TestOwnDecisionMatching:
    """2026-08-03 — a review must be graded against ITS OWN decision,
    never a neighboring cycle's. Caught live on p210 GOOGL: the 15:38
    review's own prediction (SHORT) was still pending, so the resolved-
    only index matched a resolved HOLD from a different cycle 27 min
    away and filed the disagreement 'moot (no trade taken)'. The same
    mechanism could score a row against a neighbor's P&L outright."""

    PP_VETO = '{"symbol": "GOOGL", "verdict": "VETO"}'

    def _rows(self):
        return [(NOW, "ensemble:adversarial_reviewer", "anthropic",
                 "haiku", "ALLOW", 0, None, 0.001, 900, self.PP_VETO)]

    def test_pending_own_decision_stays_pending_not_moot(self, tmp_path):
        """Own same-minute prediction pending, resolved HOLD neighbor
        9 min later — the neighbor must NOT hijack the match."""
        db = _mk_profile(
            tmp_path, "401", shadow_rows=self._rows(),
            predictions=[
                ("GOOGL", "2026-07-25 14:00:00", None, "pending", "SHORT"),
                ("GOOGL", "2026-07-25 14:09:00", 3.3, "resolved", "HOLD"),
            ])
        v = collect_fleet_metrics([db])["per_model"]["anthropic:haiku"]
        assert v["dis_pending"] == 1, (
            "own decision unresolved → the disagreement is PENDING")
        assert v["moot"] == 0, (
            "a neighboring cycle's resolved HOLD must never file this "
            "review as moot")
        assert v["dis_resolved"] == 0

    def test_resolved_own_decision_scores_not_the_neighbor(self, tmp_path):
        """Own same-minute prediction resolved as a TRADED short with
        -5% return; resolved HOLD neighbor nearby. Must score against
        the own traded decision (not moot)."""
        db = _mk_profile(
            tmp_path, "402", shadow_rows=self._rows(),
            predictions=[
                ("GOOGL", "2026-07-25 14:00:00", -5.0, "resolved", "SHORT"),
                ("GOOGL", "2026-07-25 14:09:00", 3.3, "resolved", "HOLD"),
            ])
        v = collect_fleet_metrics([db])["per_model"]["anthropic:haiku"]
        assert v["dis_resolved"] == 1
        assert v["moot"] == 0, "own decision WAS traded — not moot"
        assert v["edge_n"] == 1, "must be scored against the own trade"

    def test_jitter_match_still_works_without_same_cycle_row(self, tmp_path):
        """No own-minute row at all: the nearest prediction within the
        window IS the review's decision (write jitter) — unchanged."""
        db = _mk_profile(
            tmp_path, "403", shadow_rows=self._rows(),
            predictions=[
                ("GOOGL", "2026-07-25 13:58:30", -5.0, "resolved", "SHORT"),
            ])
        v = collect_fleet_metrics([db])["per_model"]["anthropic:haiku"]
        assert v["dis_resolved"] == 1
        assert v["edge_n"] == 1

    def test_exact_tie_prefers_the_resolved_twin(self, tmp_path):
        """Two same-second rows (dual-class dedupe shape): grade from
        the resolved one rather than pending forever."""
        db = _mk_profile(
            tmp_path, "404", shadow_rows=self._rows(),
            predictions=[
                ("GOOGL", "2026-07-25 14:00:00", None, "pending", "SHORT"),
                ("GOOGL", "2026-07-25 14:00:00", -5.0, "resolved", "SHORT"),
            ])
        v = collect_fleet_metrics([db])["per_model"]["anthropic:haiku"]
        assert v["dis_resolved"] == 1
        assert v["edge_n"] == 1
