"""2026-09-20 — a failed AI call is not a decision.

When the apex batch call failed (provider 429/5xx, unparseable output)
or was cost-capped, `ai_select_trades` returned a stand-in with an
empty trade list and the pipeline's "record a prediction for every
candidate the AI analyzed" loop journaled HOLD for every candidate —
8,187 HOLD predictions no model made over Experiment 2's first four
weeks (the Gemini arms lost ~21% of their cycles to quota errors),
resolved and graded like real ones: 20-32% of all resolved HOLDs on
those arms.

Pinned here: the stand-in is flagged, the pipeline records nothing for
it, and the rows already journaled can be moved out of every learning
consumer's sight — reversibly — by the quarantine script.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAILED = {"trades": [], "alternates": [], "pass_this_cycle": True,
          "call_failed": True,
          "portfolio_reasoning": "AI call failed: 429 RESOURCE_EXHAUSTED. "
                                 "Your project has exceeded its monthly "
                                 "spending cap."}
CAPPED = {"trades": [], "alternates": [], "pass_this_cycle": True,
          "cost_capped": True,
          "portfolio_reasoning": "Cost cap reached — no new trades."}
REAL_PASS = {"trades": [], "alternates": [], "pass_this_cycle": True,
             "portfolio_reasoning": "Nothing here clears the bar."}


class TestStandInIsFlagged:
    def test_is_no_decision(self):
        from ai_analyst import is_no_decision
        assert is_no_decision(FAILED) and is_no_decision(CAPPED)
        # a genuine "pass" IS a decision: HOLD on everything shown
        assert not is_no_decision(REAL_PASS)
        assert not is_no_decision({"trades": [{"symbol": "A"}]})
        assert not is_no_decision(None) and not is_no_decision("x")

    def test_a_failing_provider_call_returns_the_flagged_stand_in(
            self, monkeypatch):
        import ai_analyst

        def boom(*a, **k):
            raise RuntimeError("429 RESOURCE_EXHAUSTED")
        monkeypatch.setattr(ai_analyst, "call_ai", boom)
        monkeypatch.setattr(ai_analyst, "_build_batch_prompt",
                            lambda *a, **k: "prompt")
        resp = ai_analyst.ai_select_trades(
            [{"symbol": "AAA"}], {}, {}, ctx=None)
        assert resp["trades"] == [] and resp["call_failed"] is True
        assert ai_analyst.is_no_decision(resp)

    def test_a_cost_capped_call_is_a_no_decision_too(self, monkeypatch):
        import ai_analyst
        import cost_guard
        from cost_guard import CostCapExceeded
        # the real recommendation text reads the spend ledger
        monkeypatch.setattr(cost_guard, "format_cost_recommendation",
                            lambda *a, **k: "daily ceiling reached")

        def capped(*a, **k):
            raise CostCapExceeded(1, 0.25, "batch_select")
        monkeypatch.setattr(ai_analyst, "call_ai", capped)
        monkeypatch.setattr(ai_analyst, "_build_batch_prompt",
                            lambda *a, **k: "prompt")
        resp = ai_analyst.ai_select_trades(
            [{"symbol": "AAA"}], {}, {}, ctx=None)
        assert resp.get("cost_capped") is True
        assert ai_analyst.is_no_decision(resp)


class TestPipelineRecordsNothing:
    def test_the_record_every_candidate_loop_is_gated(self):
        """Structural pin at the only call site: between "Record a
        prediction for EVERY candidate" and the meta-model step, the
        loop must iterate a list that is emptied on a no-decision
        response — never `candidates_data` directly."""
        src = open(os.path.join(REPO, "trade_pipeline.py")).read()
        i = src.index("Record a prediction for EVERY candidate")
        j = src.index("STEP 4.5: Meta-model re-weighting")
        block = src[i:j]
        assert "is_no_decision(ai_response)" in block
        assert "_recordable = []" in block
        assert "for c in _recordable:" in block
        assert "for c in candidates_data:" not in block
        assert block.index("is_no_decision(ai_response)") < block.index(
            "pred_id = record_prediction(")

    def test_there_is_still_exactly_one_prediction_recording_site(self):
        """If a second site appears it needs the same gate."""
        hits = []
        for name in os.listdir(REPO):
            if name.endswith(".py"):
                text = open(os.path.join(REPO, name)).read()
                if "record_prediction(" in text and name != "ai_tracker.py":
                    hits.append(name)
        assert hits == ["trade_pipeline.py"], hits


# ---------------------------------------------------------------------------
# The Learning Scoreboard shows how blind each arm was
# ---------------------------------------------------------------------------

def _scoreboard_db(tmp_path, with_cycles=True):
    db = str(tmp_path / "quantopsai_profile_9.db")
    c = sqlite3.connect(db)
    c.execute("""CREATE TABLE ai_predictions (
        id INTEGER PRIMARY KEY, timestamp TEXT, predicted_signal TEXT,
        confidence REAL, actual_return_pct REAL, status TEXT,
        actual_outcome TEXT, data_quality TEXT, ai_model TEXT)""")
    c.execute("INSERT INTO ai_predictions (timestamp, predicted_signal, "
              "confidence, actual_return_pct, status, actual_outcome) "
              "VALUES ('2026-09-08 14:00:00', 'BUY', 70, 3.0, 'resolved', "
              "'win')")
    if with_cycles:
        c.execute("CREATE TABLE ai_cycles (cycle_id TEXT, timestamp TEXT, "
                  "raw_response_json TEXT)")
        mentions = {"trades": [{"symbol": "A", "action": "BUY"}],
                    "portfolio_reasoning": "AI call failed earlier per "
                                           "the news; buying."}
        for i, (ts, resp) in enumerate([
                ("2026-09-08 14:00:00", REAL_PASS),
                ("2026-09-08 15:00:00", FAILED),
                ("2026-09-09 14:00:00", CAPPED),
                ("2026-09-09 15:00:00", mentions),
                ("2026-09-15 14:00:00", FAILED)]):
            c.execute("INSERT INTO ai_cycles VALUES (?, ?, ?)",
                      (f"c{i}", ts, json.dumps(resp)))
    c.commit()
    c.close()
    return db


class TestScoreboardLostCycles:
    def test_lost_cycles_are_counted_per_week_by_shape(self, tmp_path):
        from learning_scoreboard import (_finalize_week,
                                         profile_weekly_predictions)
        raw = profile_weekly_predictions(_scoreboard_db(tmp_path))
        w37 = _finalize_week(raw["2026-W37"])
        # 4 cycles that week; the failed and the cost-capped one are
        # lost, the genuine pass and the answer that merely MENTIONS a
        # failure are not
        assert (w37["cycles"], w37["lost_cycles"], w37["lost_pct"]) == (
            4, 2, 50.0)
        w38 = _finalize_week(raw["2026-W38"])
        assert (w38["cycles"], w38["lost_cycles"], w38["lost_pct"]) == (
            1, 1, 100.0)
        assert w37["n"] == 1            # prediction scoring is unchanged

    def test_a_journal_without_cycles_shows_a_dash_not_zero(self, tmp_path):
        from learning_scoreboard import (_finalize_week,
                                         profile_weekly_predictions)
        raw = profile_weekly_predictions(
            _scoreboard_db(tmp_path, with_cycles=False))
        w = _finalize_week(raw["2026-W37"])
        assert w["cycles"] == 0 and w["lost_pct"] is None

    def test_arm_totals_sum_the_replicates(self, tmp_path):
        from learning_scoreboard import collect_scoreboard
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        dbs = {1: _scoreboard_db(a), 2: _scoreboard_db(b)}
        for db in dbs.values():
            sqlite3.connect(db).execute(
                "CREATE TABLE daily_snapshots (date TEXT, equity REAL)"
            ).connection.commit()
        profiles = [{"id": i, "name": f"p{i}", "strategy_type": "ai",
                     "ai_provider": "x", "ai_model": None, "enabled": 1}
                    for i in dbs]
        board = collect_scoreboard(profiles, dbs.get,
                                   spy_fetch=lambda *a, **k: {})
        wk = board["arms"]["x:None"]["weeks"]["2026-W37"]
        assert (wk["cycles"], wk["lost_cycles"], wk["lost_pct"]) == (
            8, 4, 50.0)

    def test_the_page_has_the_column_and_explains_it(self):
        html = open(os.path.join(REPO, "templates", "learning.html")).read()
        assert ">No decision</th>" in html
        assert "w.lost_cycles" in html and "w.cycles" in html
        assert "A blind arm is not a cautious one" in html


# ---------------------------------------------------------------------------
# The quarantine script
# ---------------------------------------------------------------------------

def _script():
    path = os.path.join(
        REPO, "scripts", "quarantine_no_decision_predictions_2026_09_20.py")
    spec = importlib.util.spec_from_file_location("quarantine_nd", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _journal(tmp_path):
    db = str(tmp_path / "quantopsai_profile_1.db")
    c = sqlite3.connect(db)
    c.executescript("""
        CREATE TABLE ai_cycles (cycle_id TEXT PRIMARY KEY,
                                raw_response_json TEXT);
        CREATE TABLE ai_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id TEXT,
            symbol TEXT, predicted_signal TEXT, status TEXT);
        CREATE TABLE ai_prediction_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, prediction_id INTEGER,
            horizon TEXT);
        CREATE TABLE specialist_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, prediction_id INTEGER,
            specialist_name TEXT, verdict TEXT);
    """)
    cycles = {
        "failed": FAILED, "capped": CAPPED, "pass": REAL_PASS,
        "real": {"trades": [{"symbol": "AAA", "action": "BUY"}],
                 "portfolio_reasoning": "AI call failed earlier per the "
                                        "news; buying the dip."}}
    for cid, resp in cycles.items():
        c.execute("INSERT INTO ai_cycles VALUES (?, ?)",
                  (cid, json.dumps(resp)))
    rows = [("failed", "AAA", "HOLD"), ("failed", "BBB", "HOLD"),
            ("capped", "CCC", "HOLD"),
            ("pass", "DDD", "HOLD"),                 # a REAL hold
            ("real", "AAA", "BUY"), ("real", "EEE", "HOLD")]
    for cid, sym, sig in rows:
        pid = c.execute(
            "INSERT INTO ai_predictions (cycle_id, symbol, "
            "predicted_signal, status) VALUES (?, ?, ?, 'resolved')",
            (cid, sym, sig)).lastrowid
        c.execute("INSERT INTO ai_prediction_outcomes "
                  "(prediction_id, horizon) VALUES (?, '5d')", (pid,))
        c.execute("INSERT INTO specialist_outcomes (prediction_id, "
                  "specialist_name, verdict) VALUES (?, 'risk', 'ABSTAIN')",
                  (pid,))
    c.commit()
    c.close()
    return db


def _counts(db):
    c = sqlite3.connect(db)
    try:
        tables = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in sorted(tables) if t != "sqlite_sequence"}
    finally:
        c.close()


class TestQuarantineScript:
    def test_dry_run_counts_and_writes_nothing(self, tmp_path):
        q, db = _script(), _journal(tmp_path)
        before = _counts(db)
        counts = q.quarantine(db, apply=False)
        assert counts == {"predictions": 3, "left_alone": {},
                          "ai_prediction_outcomes": 3,
                          "specialist_outcomes": 3}
        assert _counts(db) == before

    def test_apply_moves_only_fabricated_holds_with_their_dependents(
            self, tmp_path):
        q, db = _script(), _journal(tmp_path)
        q.quarantine(db, apply=True)
        c = sqlite3.connect(db)
        live = c.execute("SELECT cycle_id, symbol FROM ai_predictions "
                         "ORDER BY id").fetchall()
        # the genuine pass and the real cycle (whose reasoning merely
        # MENTIONS a failed call) are untouched
        assert live == [("pass", "DDD"), ("real", "AAA"), ("real", "EEE")]
        moved = c.execute(
            "SELECT symbol, quarantine_reason, quarantined_at IS NOT NULL "
            "FROM ai_predictions_no_decision ORDER BY id").fetchall()
        assert moved == [("AAA", q.REASON, 1), ("BBB", q.REASON, 1),
                         ("CCC", q.REASON, 1)]
        for t in ("ai_prediction_outcomes", "specialist_outcomes"):
            assert c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 3
            assert c.execute(
                f"SELECT COUNT(*) FROM {t}_no_decision").fetchone()[0] == 3
            # no dependent is left pointing at a prediction that moved
            assert c.execute(
                f"SELECT COUNT(*) FROM {t} WHERE prediction_id NOT IN "
                "(SELECT id FROM ai_predictions)").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM ai_cycles").fetchone()[0] == 4
        c.close()

    def test_apply_is_idempotent_and_restore_is_exact(self, tmp_path):
        q, db = _script(), _journal(tmp_path)
        before = _counts(db)
        ids_before = sqlite3.connect(db).execute(
            "SELECT id, cycle_id, symbol FROM ai_predictions ORDER BY id"
        ).fetchall()
        q.quarantine(db, apply=True)
        again = q.quarantine(db, apply=True)
        assert again["predictions"] == 0
        assert q.restore(db, apply=False)["ai_predictions"] == 3
        q.restore(db, apply=True)
        after = _counts(db)
        assert {k: v for k, v in after.items()
                if not k.endswith("_no_decision")} == before
        assert all(v == 0 for k, v in after.items()
                   if k.endswith("_no_decision"))
        assert sqlite3.connect(db).execute(
            "SELECT id, cycle_id, symbol FROM ai_predictions ORDER BY id"
        ).fetchall() == ids_before

    def test_a_non_hold_row_on_a_failed_cycle_is_reported_not_moved(
            self, tmp_path):
        q, db = _script(), _journal(tmp_path)
        c = sqlite3.connect(db)
        c.execute("INSERT INTO ai_predictions (cycle_id, symbol, "
                  "predicted_signal, status) VALUES "
                  "('failed', 'ZZZ', 'BUY', 'resolved')")
        c.commit()
        c.close()
        counts = q.quarantine(db, apply=True)
        assert counts["left_alone"] == {"BUY": 1}
        assert sqlite3.connect(db).execute(
            "SELECT COUNT(*) FROM ai_predictions WHERE symbol='ZZZ'"
        ).fetchone()[0] == 1

    def test_a_journal_without_the_tables_is_a_clean_no_op(self, tmp_path):
        q = _script()
        db = str(tmp_path / "empty.db")
        sqlite3.connect(db).close()
        assert q.quarantine(db, apply=True)["predictions"] == 0

    def test_default_run_is_a_dry_run(self, tmp_path, capsys):
        q, db = _script(), _journal(tmp_path)
        before = _counts(db)
        assert q.main(["--db", db]) == 0
        assert "dry run" in capsys.readouterr().out
        assert _counts(db) == before
