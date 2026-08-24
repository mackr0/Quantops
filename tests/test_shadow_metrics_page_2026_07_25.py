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
            latency_ms INTEGER, primary_parsed TEXT, decision_id TEXT
        )""")
    conn.execute("""
        CREATE TABLE ai_predictions (
            id INTEGER PRIMARY KEY, symbol TEXT, timestamp TEXT,
            status TEXT, actual_return_pct REAL, predicted_signal TEXT,
            decision_id TEXT
        )""")
    for r in shadow_rows:
        # 10-tuple = legacy row (decision_id NULL); 11-tuple carries it.
        r = tuple(r) + (None,) if len(r) == 10 else tuple(r)
        conn.execute(
            "INSERT INTO ai_shadow_calls (timestamp, purpose, provider,"
            " model, parsed_signal, agreement, error, cost_usd,"
            " latency_ms, primary_parsed, decision_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            r)
    for p in predictions:
        # 3-tuple (sym, ts, ret) = legacy resolved row; 5-tuple adds
        # explicit (status, predicted_signal); 6-tuple adds decision_id.
        did = None
        if len(p) == 3:
            sym, ts, ret = p
            status, psig = "resolved", None
        elif len(p) == 5:
            sym, ts, ret, status, psig = p
        else:
            sym, ts, ret, status, psig, did = p
        conn.execute(
            "INSERT INTO ai_predictions (symbol, timestamp, status,"
            " actual_return_pct, predicted_signal, decision_id)"
            " VALUES (?,?,?,?,?,?)",
            (sym, ts, status, ret, psig, did))
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
        # Uses a FORECAST specialist so the by-primary-action cut is a
        # directional bucket. Since 2026-08-07 the VETO-authority
        # specialists group under "gate: allow"/"gate: block" instead —
        # a veto decides whether a trade happens, it is not a bearish
        # forecast. See TestPageConsistency in
        # test_shadow_grading_fairness_2026_07_30.py.
        db = _mk_profile(
            tmp_path, "304",
            shadow_rows=[
                (NOW, "ensemble:pattern_recognizer", "anthropic", "haiku",
                 "SELL", 1, None, 0.001, 900, PP_SELL),
                (NOW, "transcript_sentiment", "anthropic", "haiku",
                 "NEUTRAL", 1, None, 0.001, 900,
                 '{"symbol": "AAPL", "tone": "neutral"}'),
            ],
        )
        m = collect_fleet_metrics([db])
        assert set(m["by_purpose"]) == {"ensemble:pattern_recognizer",
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


class TestExactDecisionMatching:
    """2026-08-06 — operator directive: matching by time proximity 'is
    hoping we get lucky'. Every specialist call now carries the
    candidate's decision_id, the prediction row stores the same id,
    and the scorer joins on it as an IDENTITY. Time-window matching
    survives only for rows that predate the plumbing, counted
    separately on the page."""

    PP_VETO = '{"symbol": "GOOGL", "verdict": "VETO"}'

    def _row(self, decision_id):
        return [(NOW, "ensemble:adversarial_reviewer", "anthropic",
                 "haiku", "ALLOW", 0, None, 0.001, 900, self.PP_VETO,
                 decision_id)]

    def test_exact_join_beats_a_nearer_neighbor(self, tmp_path):
        """The review's own decision (by id) is 25 min away and TRADED;
        an unrelated resolved HOLD sits at the same minute. The id must
        win — scored, not moot."""
        db = _mk_profile(
            tmp_path, "501", shadow_rows=self._row("dec-1"),
            predictions=[
                ("GOOGL", "2026-07-25 14:00:10", 3.3, "resolved",
                 "HOLD", None),                       # nearer, unrelated
                ("GOOGL", "2026-07-25 14:25:00", -5.0, "resolved",
                 "SHORT", "dec-1"),                    # its own decision
            ])
        v = collect_fleet_metrics([db])["per_model"]["anthropic:haiku"]
        assert v["dis_resolved"] == 1
        assert v["moot"] == 0, (
            "the identity join must beat the nearer unrelated HOLD")
        assert v["edge_n"] == 1
        assert v["match_exact"] == 1 and v["match_window"] == 0

    def test_exact_pending_never_falls_back_to_a_neighbor(self, tmp_path):
        db = _mk_profile(
            tmp_path, "502", shadow_rows=self._row("dec-2"),
            predictions=[
                ("GOOGL", "2026-07-25 14:00:10", 3.3, "resolved",
                 "HOLD", None),
                ("GOOGL", "2026-07-25 14:00:00", None, "pending",
                 "SHORT", "dec-2"),
            ])
        v = collect_fleet_metrics([db])["per_model"]["anthropic:haiku"]
        assert v["dis_pending"] == 1 and v["dis_resolved"] == 0

    def test_legacy_row_without_id_uses_window_and_is_counted(self, tmp_path):
        db = _mk_profile(
            tmp_path, "503", shadow_rows=self._row(None),
            predictions=[
                ("GOOGL", "2026-07-25 14:00:10", -5.0, "resolved",
                 "SHORT", None),
            ])
        v = collect_fleet_metrics([db])["per_model"]["anthropic:haiku"]
        assert v["dis_resolved"] == 1
        assert v["match_window"] == 1 and v["match_exact"] == 0

    def test_id_on_row_but_no_prediction_falls_back(self, tmp_path):
        """Prediction write failed/raced: the shadow row carries an id
        nothing else has — fall back to the window rather than lose
        the evidence."""
        db = _mk_profile(
            tmp_path, "504", shadow_rows=self._row("dec-orphan"),
            predictions=[
                ("GOOGL", "2026-07-25 14:00:10", -5.0, "resolved",
                 "SHORT", None),
            ])
        v = collect_fleet_metrics([db])["per_model"]["anthropic:haiku"]
        assert v["dis_resolved"] == 1
        assert v["match_window"] == 1


REPO_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), ".."))


class TestDecisionIdPlumbing:
    """Structural: the id must ride every hop — ensemble mint →
    call_ai → dispatch_shadow_calls → row write → record_prediction."""

    def test_ensemble_mints_and_threads(self):
        src = open(os.path.join(REPO_DIR, "ensemble.py")).read()
        assert '_c["_decision_id"] = _uuid.uuid4().hex' in src
        # 2026-08-23: ONE structured path for every provider (docs/25
        # item 1.2) — exactly one specialist call site carries the id.
        assert src.count("decision_id=_chunk_decision_id") == 1, (
            "both the tool-use and plain-prompt specialist calls must "
            "carry the chunk's decision id")

    def test_call_ai_threads_to_dispatch(self):
        src = open(os.path.join(REPO_DIR, "ai_providers.py")).read()
        i = src.index("dispatch_shadow_calls(")
        assert "decision_id=decision_id" in src[i:i + 500]

    def test_record_prediction_persists_it(self):
        src = open(os.path.join(REPO_DIR, "ai_tracker.py")).read()
        assert "rule_votes_json, decision_id, ai_provider, ai_model)" in src, (
            "ai_predictions INSERT lost the decision_id column (or the "
            "2026-08-23 model-attribution columns)")
        src2 = open(os.path.join(REPO_DIR, "trade_pipeline.py")).read()
        assert 'decision_id=c.get("_decision_id")' in src2

    def test_migration_registry_covers_both_tables(self):
        src = open(os.path.join(REPO_DIR, "journal.py")).read()
        i = src.index('"ai_predictions": [')
        assert '("decision_id", "TEXT")' in src[i:i + 400]
        j = src.index('"ai_shadow_calls": [')
        assert '("decision_id", "TEXT")' in src[j:j + 200]


class TestVisibilityFixes20260811:
    """2026-08-11 — operator: 'most of the page only has the different
    models, not the variant' and 'pNUMBER for profile name is obtuse'.
    Pins: per-arm representation in recent disagreements (newest
    first), same-call gate block rates (the variant's process
    readout), purposes-covered scope, and display-name labels."""

    PP_VETO = '{"symbol": "GOOGL", "verdict": "VETO"}'

    def test_recent_list_represents_low_volume_arms(self, tmp_path):
        rows = []
        # 50 disagreements for the loud arm, 2 for the quiet variant
        for i in range(50):
            rows.append((f"2026-07-25 14:{i%60:02d}:00",
                         "ensemble:adversarial_reviewer", "anthropic",
                         "haiku", "ALLOW", 0, None, 0.001, 900,
                         self.PP_VETO))
        rows.append(("2026-07-25 10:00:00",
                     "ensemble:adversarial_reviewer", "google",
                     "lite@adversarial_v2", "ALLOW", 0, None, 0.001,
                     900, self.PP_VETO))
        rows.append(("2026-07-25 10:05:00",
                     "ensemble:adversarial_reviewer", "google",
                     "lite@adversarial_v2", "ALLOW", 0, None, 0.001,
                     900, self.PP_VETO))
        db = _mk_profile(tmp_path, "601", shadow_rows=rows)
        m = collect_fleet_metrics([db])
        models = {d["model"] for d in m["recent_disagreements"]}
        assert "google:lite@adversarial_v2" in models, (
            "a low-volume arm must still appear in the recent list")
        ts = [d["ts"] for d in m["recent_disagreements"]]
        assert ts == sorted(ts, reverse=True), "newest first"

    def test_gate_block_rates_same_calls(self, tmp_path):
        rows = [
            (NOW, "ensemble:adversarial_reviewer", "google",
             "lite@adversarial_v2", "ALLOW", 0, None, 0.001, 900,
             self.PP_VETO),
            (NOW, "ensemble:adversarial_reviewer", "google",
             "lite@adversarial_v2", "VETO", 1, None, 0.001, 900,
             self.PP_VETO),
        ]
        db = _mk_profile(tmp_path, "602", shadow_rows=rows)
        v = collect_fleet_metrics([db])["per_model"][
            "google:lite@adversarial_v2"]
        assert v["gate_calls"] == 2
        assert v["gate_block_pct"] == 50.0     # blocked 1 of 2
        assert v["gate_primary_block_pct"] == 100.0
        assert v["purposes_covered"] == 1

    def test_no_gate_calls_reads_unmeasured(self, tmp_path):
        db = _mk_profile(tmp_path, "603", shadow_rows=[
            (NOW, "ensemble:pattern_recognizer", "anthropic", "haiku",
             "BUY", 0, None, 0.001, 900, PP_SELL)])
        v = collect_fleet_metrics([db])["per_model"]["anthropic:haiku"]
        assert v["gate_block_pct"] is None, "never 0% for unmeasured"

    def test_profile_display_name_used(self, tmp_path):
        db = _mk_profile(tmp_path, "604", shadow_rows=[
            (NOW, "ensemble:pattern_recognizer", "anthropic", "haiku",
             "BUY", 0, None, 0.001, 900, PP_SELL)],
            predictions=[("NVDA", "2026-07-25T13:58:30", 4.0)])
        m = collect_fleet_metrics(
            [db], profile_names={db: "EXP-A2-NoAltData"})
        assert m["recent_disagreements"][0]["profile"] == \
            "EXP-A2-NoAltData"

    def test_template_carries_the_new_sections(self):
        tpl = open(os.path.join(REPO_DIR, "templates",
                                "shadow.html")).read()
        assert "Prompt-variant process" in tpl
        assert "Primary blocks (same calls)" in tpl
        assert "Scope" in tpl


class TestOutcomeColoring20260811:
    """2026-08-11 — the Shadow-won column was hardcoded green (a
    column-identity color). Normal convention: whichever side won more
    gets green, the other red, ties neutral — and edge numbers color
    by sign."""

    def test_no_static_green_on_shadow_won(self):
        tpl = open(os.path.join(REPO_DIR, "templates",
                                "shadow.html")).read()
        assert 'class="pnl-pos">{{ v.shadow_only }}' not in tpl
        assert 'class="pnl-pos"><strong>{{ v.shadow_only }}' not in tpl

    def test_comparative_classes_present(self):
        tpl = open(os.path.join(REPO_DIR, "templates",
                                "shadow.html")).read()
        assert tpl.count(
            "{% if v.shadow_only > v.primary_only %}pnl-pos"
            "{% elif v.shadow_only < v.primary_only %}pnl-neg") == 3, (
            "all three tables must color the tally by who is ahead")
        assert ("{% if v.primary_only > v.shadow_only %}pnl-pos"
                "{% elif v.primary_only < v.shadow_only %}pnl-neg"
                ) in tpl

    def test_edge_cells_color_by_sign(self):
        tpl = open(os.path.join(REPO_DIR, "templates",
                                "shadow.html")).read()
        assert ("v.edge_per_decision is not none and "
                "v.edge_per_decision > 0 %}pnl-pos") in tpl
        assert ("v.edge_per_decision is not none and "
                "v.edge_per_decision < 0 %}pnl-neg") in tpl
