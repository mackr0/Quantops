"""Shadow grading must understand every shape the ensemble emits —
including the batched structured-output {"verdicts": [...]} shape.

2026-09-16, operator: "/shadow is totally blank in terms of decisions
when we have made thousands of decisions... why are we blowing through
all this money if you can't keep the system learning." Second
occurrence of the 2026-07-24 failure class: the Experiment-2 build
(2026-08-23) moved every ensemble specialist to the batched
{"verdicts": [{symbol, verdict, confidence, ...}]} schema and
`shadow_eval._extract_signal` never learned it, so `agreement` was
None on 100% of the 37,941 shadow calls made since the 2026-08-24
restart (~$45 spent, zero comparisons ran, the page empty).

Pins:
  1. The batched shape extracts: one entry -> the bare verdict
     (byte-identical to the legacy singular shape, so the gate/stance
     cuts keep working); many entries -> sorted "SYM:VERDICT,..." set
     string graded at set level like the apex trade set.
  2. The extractor is tied to ensemble._verdicts_schema ITSELF — every
     verdict value the schema's enum allows must extract, and a
     schema-conforming response can never be unextractable again. A
     future schema change that this extractor doesn't know breaks HERE,
     not silently in production.
  3. Multi-candidate gate reviews file as "gate: set-level", never
     "gate: unrecognised".
  4. The 2026-07-24 backfill script re-derives parsed_signal +
     agreement for already-paid-for rows from stored responses (no API
     calls), is dry-run by default, and is idempotent.
"""
from __future__ import annotations

import importlib
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shadow_eval import _extract_signal, _compute_agreement  # noqa: E402


# The exact live shape from the incident rows (p233, 2026-09-15).
_INCIDENT_SINGLE = {"verdicts": [
    {"symbol": "JNJ", "verdict": "VETO", "confidence": 97,
     "reasoning": "Explicit rule veto triggered by FDA inspection..."},
]}


class TestBatchedVerdictExtraction:
    def test_single_entry_extracts_bare_verdict(self):
        # Byte-identical to the legacy singular shape, so
        # shadow_metrics' gate/stance cuts keep working unchanged.
        assert _extract_signal(_INCIDENT_SINGLE) == "VETO"
        assert _extract_signal(_INCIDENT_SINGLE) == _extract_signal(
            {"verdict": "VETO"})

    def test_multi_entry_canonical_set(self):
        a = {"verdicts": [{"symbol": "jnj", "verdict": "VETO"},
                          {"symbol": "AZO", "verdict": "hold"}]}
        b = {"verdicts": [{"symbol": "AZO", "verdict": "HOLD"},
                          {"symbol": "JNJ", "verdict": "veto"}]}
        # order-insensitive, case-normalised — same set, same string
        assert _extract_signal(a) == _extract_signal(b) == \
            "AZO:HOLD,JNJ:VETO"

    def test_empty_verdicts_is_none_marker(self):
        # An empty array is a real response (the specialist issued no
        # rulings), not an unparseable one — it must stay gradable.
        assert _extract_signal({"verdicts": []}) == "NONE"

    def test_single_entry_without_verdict_is_ungradable(self):
        assert _extract_signal(
            {"verdicts": [{"symbol": "JNJ", "confidence": 50}]}) is None

    def test_non_dict_entries_are_filtered(self):
        got = _extract_signal({"verdicts": [
            "garbage", {"symbol": "A", "verdict": "BUY"}]})
        # one usable entry left -> bare-verdict form
        assert got == "BUY"


class TestBatchedAgreement:
    def test_same_set_different_order_agrees(self):
        p = {"verdicts": [{"symbol": "A", "verdict": "BUY"},
                          {"symbol": "B", "verdict": "VETO"}]}
        s = {"verdicts": [{"symbol": "B", "verdict": "VETO"},
                          {"symbol": "A", "verdict": "BUY"}]}
        assert _compute_agreement(p, s) == 1

    def test_symbol_hallucination_cannot_score_agreement(self):
        # Same verdicts on DIFFERENT symbols is a disagreement — the
        # symbol stays inside the multi-entry canonical string exactly
        # so this can never grade as a match.
        p = {"verdicts": [{"symbol": "A", "verdict": "VETO"},
                          {"symbol": "B", "verdict": "HOLD"}]}
        s = {"verdicts": [{"symbol": "A", "verdict": "VETO"},
                          {"symbol": "C", "verdict": "HOLD"}]}
        assert _compute_agreement(p, s) == 0

    def test_batched_vs_legacy_singular_agree(self):
        # A primary on the batched schema and a shadow that answered in
        # the legacy singular shape (schema obedience differs by
        # vendor) still compare — both canonicalise to the bare verdict.
        assert _compute_agreement(_INCIDENT_SINGLE,
                                  {"verdict": "veto"}) == 1
        assert _compute_agreement(_INCIDENT_SINGLE,
                                  {"verdict": "HOLD"}) == 0


class TestSchemaTie:
    """The extractor is pinned to the ensemble's OWN schema object —
    schema drift breaks here, in tests, not silently in production."""

    def test_every_schema_verdict_value_extracts(self):
        from ensemble import _verdicts_schema
        schema = _verdicts_schema()
        item = schema["properties"]["verdicts"]["items"]
        enum = item["properties"]["verdict"]["enum"]
        assert enum, "ensemble verdict enum vanished from the schema"
        for value in enum:
            single = {"verdicts": [
                {"symbol": "TEST", "verdict": value, "confidence": 50}]}
            assert _extract_signal(single) == value.upper(), (
                f"schema-legal verdict {value!r} is unextractable — "
                "agreement would silently be None on real calls again")

    def test_schema_conforming_multi_response_extracts(self):
        from ensemble import _verdicts_schema
        enum = (_verdicts_schema()["properties"]["verdicts"]["items"]
                ["properties"]["verdict"]["enum"])
        multi = {"verdicts": [
            {"symbol": f"S{i}", "verdict": v, "confidence": 50}
            for i, v in enumerate(enum)]}
        got = _extract_signal(multi)
        assert got is not None
        assert all(f"S{i}:{v}" in got for i, v in enumerate(enum))

    def test_container_key_is_the_schema_container_key(self):
        # If someone renames the top-level container in the schema, the
        # extractor must be taught the new name in the same commit.
        from ensemble import _verdicts_schema
        (container,) = _verdicts_schema()["required"]
        assert _extract_signal(
            {container: [{"symbol": "A", "verdict": "HOLD"}]}
        ) is not None, (
            f"schema container {container!r} is not extractable")


class TestGateSetLevelBucket:
    def test_multi_candidate_gate_review_files_as_set_level(self, tmp_path):
        from shadow_metrics import collect_fleet_metrics
        db = str(tmp_path / "quantopsai_profile_9901.db")
        conn = sqlite3.connect(db)
        conn.execute("""
            CREATE TABLE ai_shadow_calls (
                id INTEGER PRIMARY KEY, timestamp TEXT, purpose TEXT,
                provider TEXT, model TEXT, parsed_signal TEXT,
                agreement INTEGER, error TEXT, cost_usd REAL,
                latency_ms INTEGER, primary_parsed TEXT,
                decision_id TEXT
            )""")
        conn.execute("""
            CREATE TABLE ai_predictions (
                id INTEGER PRIMARY KEY, symbol TEXT, timestamp TEXT,
                status TEXT, actual_return_pct REAL,
                predicted_signal TEXT, decision_id TEXT
            )""")
        primary = json.dumps({"verdicts": [
            {"symbol": "AAA", "verdict": "VETO", "confidence": 90},
            {"symbol": "BBB", "verdict": "HOLD", "confidence": 60}]})
        conn.execute(
            "INSERT INTO ai_shadow_calls (timestamp, purpose, provider,"
            " model, parsed_signal, agreement, error, cost_usd,"
            " latency_ms, primary_parsed) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("2026-09-15 14:00:00", "ensemble:adversarial_reviewer",
             "google", "gemini-test", "AAA:VETO,BBB:HOLD", 1, None,
             0.001, 500, primary))
        conn.commit(); conn.close()

        m = collect_fleet_metrics([db])
        assert m["overview"]["graded"] == 1
        actions = set(m["by_primary_action"].keys())
        assert "gate: set-level" in actions, (
            f"multi-candidate gate review filed as {actions} — a set "
            "of gate rulings must never read as 'unrecognised'")
        assert "gate: unrecognised" not in actions


class TestBackfillScript:
    """The 2026-07-24 backfill re-derives the columns for rows already
    paid for — re-run as-is for this incident's 38K rows."""

    def _mk_db(self, tmp_path):
        db_path = tmp_path / "quantopsai_profile_9902.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE ai_shadow_calls (
                id INTEGER PRIMARY KEY, raw_response TEXT,
                primary_parsed TEXT, parsed_signal TEXT,
                agreement INTEGER, error TEXT
            )""")
        primary = json.dumps({"verdicts": [
            {"symbol": "JNJ", "verdict": "VETO", "confidence": 97}]})
        rows = [
            # agreeing shadow (legacy singular shape from the shadow)
            (json.dumps({"verdicts": [
                {"symbol": "JNJ", "verdict": "VETO",
                 "confidence": 90}]}), primary, None, None, None),
            # disagreeing shadow
            (json.dumps({"verdicts": [
                {"symbol": "JNJ", "verdict": "HOLD",
                 "confidence": 55}]}), primary, None, None, None),
            # errored row — must never be touched
            (None, primary, None, None, "429 RESOURCE_EXHAUSTED"),
            # already-graded row — must never be re-touched
            ('{"verdict": "BUY"}', '{"verdict": "BUY"}', "BUY", 1, None),
        ]
        conn.executemany(
            "INSERT INTO ai_shadow_calls (raw_response, primary_parsed,"
            " parsed_signal, agreement, error) VALUES (?,?,?,?,?)",
            rows)
        conn.commit(); conn.close()
        return str(db_path)

    def _run(self, tmp_path, monkeypatch, *argv):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(sys, "argv", ["backfill"] + list(argv))
        mod = importlib.import_module(
            "scripts.backfill_shadow_agreement_2026_07_24")
        return mod.main()

    def test_dry_run_default_writes_nothing(self, tmp_path, monkeypatch):
        db = self._mk_db(tmp_path)
        assert self._run(tmp_path, monkeypatch) == 0
        conn = sqlite3.connect(db)
        n_graded = conn.execute(
            "SELECT COUNT(*) FROM ai_shadow_calls "
            "WHERE agreement IS NOT NULL").fetchone()[0]
        conn.close()
        assert n_graded == 1   # only the pre-graded row

    def test_apply_grades_and_is_idempotent(self, tmp_path, monkeypatch):
        db = self._mk_db(tmp_path)
        assert self._run(tmp_path, monkeypatch, "--apply") == 0
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT id, parsed_signal, agreement, error "
            "FROM ai_shadow_calls ORDER BY id").fetchall()
        conn.close()
        assert rows[0][1:3] == ("VETO", 1)     # agree
        assert rows[1][1:3] == ("HOLD", 0)     # disagree
        assert rows[2][1:3] == (None, None)    # errored: untouched
        assert rows[3][1:3] == ("BUY", 1)      # pre-graded: untouched
        # Idempotent: a second apply finds nothing left to grade and
        # changes nothing.
        assert self._run(tmp_path, monkeypatch, "--apply") == 0
        conn = sqlite3.connect(db)
        again = conn.execute(
            "SELECT id, parsed_signal, agreement, error "
            "FROM ai_shadow_calls ORDER BY id").fetchall()
        conn.close()
        assert again == rows


def _mk_scoring_db(tmp_path):
    """One profile DB with four batched disagreements + the resolved
    predictions they should score against (same-day follow-up: the
    funnel reached 'graded' but died at '0 scored against real trade
    outcomes' — batched rows carried no symbol and no decision id)."""
    db = str(tmp_path / "quantopsai_profile_9903.db")
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
            status TEXT, actual_return_pct REAL,
            predicted_signal TEXT, decision_id TEXT
        )""")
    ts = "2026-09-15 14:00:00"

    def _shadow(purpose, verdicts, shadow_sig):
        conn.execute(
            "INSERT INTO ai_shadow_calls (timestamp, purpose, provider,"
            " model, parsed_signal, agreement, error, cost_usd,"
            " latency_ms, primary_parsed) VALUES (?,?,?,?,?,0,NULL,"
            "0.001,500,?)",
            (ts, purpose, "google", "gemini-test", shadow_sig,
             json.dumps({"verdicts": verdicts})))

    def _pred(symbol, ret, psig):
        conn.execute(
            "INSERT INTO ai_predictions (symbol, timestamp, status,"
            " actual_return_pct, predicted_signal) VALUES "
            "(?,?, 'resolved', ?, ?)", (symbol, ts, ret, psig))

    # 1. Gate multi-set: disagree on AAA only (BBB agrees). Trade was
    #    taken (BUY) and won +5% -> primary's VETO wrong, shadow's
    #    HOLD (allow) right.
    _shadow("ensemble:adversarial_reviewer",
            [{"symbol": "AAA", "verdict": "VETO"},
             {"symbol": "BBB", "verdict": "HOLD"}],
            "AAA:HOLD,BBB:HOLD")
    _pred("AAA", 5.0, "BUY")
    # 2. Gate multi-set, no trade taken (predicted HOLD) -> moot.
    _shadow("ensemble:risk_assessor",
            [{"symbol": "HHH", "verdict": "VETO"},
             {"symbol": "III", "verdict": "HOLD"}],
            "HHH:HOLD,III:HOLD")
    _pred("HHH", 4.0, "HOLD")
    # 3. Forecast multi-set: CCC fell 3% -> shadow's SELL right,
    #    primary's BUY wrong.
    _shadow("ensemble:pattern_recognizer",
            [{"symbol": "CCC", "verdict": "BUY"},
             {"symbol": "DDD", "verdict": "HOLD"}],
            "CCC:SELL,DDD:HOLD")
    _pred("CCC", -3.0, "SHORT")
    # 4. Single-candidate batched primary vs bare shadow verdict:
    #    symbol must come from inside the verdicts array.
    _shadow("ensemble:earnings_analyst",
            [{"symbol": "EEE", "verdict": "BUY", "confidence": 70}],
            "SELL")
    _pred("EEE", -3.0, "HOLD")
    conn.commit(); conn.close()
    return db


class TestPrimarySymbol:
    def test_legacy_top_level(self):
        from shadow_metrics import _primary_symbol
        assert _primary_symbol({"symbol": "JNJ", "verdict": "X"}) == "JNJ"

    def test_single_batched_entry(self):
        from shadow_metrics import _primary_symbol
        assert _primary_symbol(_INCIDENT_SINGLE) == "JNJ"

    def test_multi_batched_has_no_single_symbol(self):
        from shadow_metrics import _primary_symbol
        assert _primary_symbol({"verdicts": [
            {"symbol": "A", "verdict": "X"},
            {"symbol": "B", "verdict": "Y"}]}) is None

    def test_junk(self):
        from shadow_metrics import _primary_symbol
        assert _primary_symbol(None) is None
        assert _primary_symbol({"verdicts": "nope"}) is None


class TestEnsembleSetOutcomeScoring:
    def test_batched_disagreements_score_against_outcomes(self, tmp_path):
        from shadow_metrics import collect_fleet_metrics
        db = _mk_scoring_db(tmp_path)
        m = collect_fleet_metrics([db])
        v = m["per_model"]["google:gemini-test"]
        # AAA (gate), CCC (forecast), EEE (single-candidate) scored;
        # HHH moot; BBB/DDD/III agreed so never candidates.
        assert v["dis_resolved"] == 3
        assert v["moot"] == 1
        assert v["shadow_only"] == 3      # shadow right on all three
        assert v["primary_right"] == 0
        assert v["dis_units"] == 3        # AAA, CCC, EEE distinct
        assert v["match_window"] == 3     # no decision ids -> window
        # The funnel that read "0 scored" on 2026-09-16 must now
        # produce a nonzero head-to-head.
        assert v["h2h_n"] == 3
        assert v["shadow_win_pct"] == 100.0

    def test_verdict_set_rows_render_scored_counts(self, tmp_path):
        from shadow_metrics import collect_fleet_metrics
        db = _mk_scoring_db(tmp_path)
        m = collect_fleet_metrics([db])
        vs_rows = [d for d in m["recent_disagreements"]
                   if d["symbol"] == "(verdict set)"]
        assert len(vs_rows) == 3   # the three multi-set rows
        assert {d["who_right"] for d in vs_rows} == {
            "1 symbols scored", "pending"}

    def test_dropped_symbol_is_never_graded(self, tmp_path):
        """A symbol only ONE side verdicted (dropped by the other) is
        schema disobedience, not a gradable decision — unlike
        batch_select where omission is a deliberate pass."""
        from shadow_metrics import collect_fleet_metrics
        db = str(tmp_path / "quantopsai_profile_9904.db")
        conn = sqlite3.connect(db)
        conn.execute("""
            CREATE TABLE ai_shadow_calls (
                id INTEGER PRIMARY KEY, timestamp TEXT, purpose TEXT,
                provider TEXT, model TEXT, parsed_signal TEXT,
                agreement INTEGER, error TEXT, cost_usd REAL,
                latency_ms INTEGER, primary_parsed TEXT,
                decision_id TEXT
            )""")
        conn.execute("""
            CREATE TABLE ai_predictions (
                id INTEGER PRIMARY KEY, symbol TEXT, timestamp TEXT,
                status TEXT, actual_return_pct REAL,
                predicted_signal TEXT, decision_id TEXT
            )""")
        ts = "2026-09-15 14:00:00"
        conn.execute(
            "INSERT INTO ai_shadow_calls (timestamp, purpose, provider,"
            " model, parsed_signal, agreement, error, cost_usd,"
            " latency_ms, primary_parsed) VALUES (?,?,?,?,?,0,NULL,"
            "0.001,500,?)",
            (ts, "ensemble:pattern_recognizer", "google", "gemini-test",
             "XXX:BUY,YYY:BUY",
             json.dumps({"verdicts": [
                 {"symbol": "XXX", "verdict": "BUY"},
                 {"symbol": "YYY", "verdict": "BUY"},
                 {"symbol": "ZZZ", "verdict": "SELL"}]})))
        conn.execute(
            "INSERT INTO ai_predictions (symbol, timestamp, status,"
            " actual_return_pct, predicted_signal) VALUES "
            "('ZZZ', ?, 'resolved', -5.0, 'SHORT')", (ts,))
        conn.commit(); conn.close()
        m = collect_fleet_metrics([db])
        v = m["per_model"]["google:gemini-test"]
        # ZZZ resolved and would score — but the shadow never answered
        # it, so nothing may be graded (XXX/YYY agree).
        assert v["dis_resolved"] == 0
        assert v["ungradable"] == 0
