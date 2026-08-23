"""Shadow-eval measurement fixes — 2026-07-30.

The operator asked which shadow model was making better decisions. The
first answer off `/shadow` was WRONG: claude-haiku-4-5 appeared to beat
the gemini-3.1-flash-lite primary 27-5 on ensemble:adversarial_reviewer.
Two measurement artifacts produced that, and both are pinned here.

1. NEUTRAL WAS UNGRADABLE. `grade()` returned None for a neutral
   stance, so whenever the primary said HOLD and the shadow took a
   direction, the shadow could score and the primary structurally could
   not. De-duplicated, haiku's veto precision was 53.8% against a 51.4%
   base rate — no edge at all. The scoring must never let one side
   answer a question the other side is barred from answering.

2. REPEAT REVIEWS LOOKED INDEPENDENT. 68 resolved reviewer rows were
   really 35 distinct (profile, symbol) pairs over 26 symbols, with CVX
   and TSLA each appearing 9 times. The page now reports `dis_units` so
   an effective sample size of a few dozen can't be mistaken for a few
   hundred observations.

Plus the join fix that made the data usable at all: a 300s match window
matched only 22.6% of reviewer calls that HAD a resolved prediction for
the symbol. 1800s recovers roughly half again as many outcomes
(fleet-wide resolved disagreements 164 -> 243) while staying inside one
trading cycle.

And the prompt-variant arm those fixes exist to serve: same model as
the primary, revised instructions, so the A/B isolates the PROMPT.
"""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import prompt_variants  # noqa: E402
import shadow_metrics  # noqa: E402
from shadow_metrics import collect_fleet_metrics  # noqa: E402


NOW = "2026-07-30 14:00:00"
PP_HOLD = '{"symbol": "CVX", "verdict": "HOLD"}'
PP_SELL = '{"symbol": "CVX", "verdict": "SELL"}'


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
    for pred in predictions:
        # 4th element is the PROPOSED trade direction; default 'HOLD'
        # means the system passed — no position was ever opened.
        sym, ts, ret = pred[0], pred[1], pred[2]
        psig = pred[3] if len(pred) > 3 else "HOLD"
        conn.execute(
            "INSERT INTO ai_predictions (symbol, timestamp, status,"
            " actual_return_pct, predicted_signal)"
            " VALUES (?, ?, 'resolved', ?, ?)",
            (sym, ts, ret, psig))
    conn.commit()
    conn.close()
    return db


class TestNeutralIsGradable:
    """The artifact that faked a 27-5 edge."""

    def test_primary_hold_can_lose_and_shadow_scores(self, tmp_path):
        # Primary HOLD, shadow VETO, price fell 6% — a real miss by the
        # primary. The shadow earns the point on the merits.
        db = _mk_profile(
            tmp_path, "401",
            shadow_rows=[(NOW, "ensemble:pattern_recognizer", "anthropic",
                          "haiku", "VETO", 0, None, 0.001, 900, PP_HOLD)],
            predictions=[("CVX", "2026-07-30T14:02:00", -6.0)],
        )
        v = collect_fleet_metrics([db])["per_model"]["anthropic:haiku"]
        assert (v["shadow_only"], v["primary_only"]) == (1, 0)

    def test_primary_hold_WINS_when_the_move_was_flat(self, tmp_path):
        """The case that was previously unscoreable — and the whole
        reason a decisive model looked good. Primary HOLD, shadow VETO,
        price barely moved: sitting out was correct."""
        db = _mk_profile(
            tmp_path, "402",
            shadow_rows=[(NOW, "ensemble:pattern_recognizer", "anthropic",
                          "haiku", "VETO", 0, None, 0.001, 900, PP_HOLD)],
            predictions=[("CVX", "2026-07-30T14:02:00", -0.4)],
        )
        v = collect_fleet_metrics([db])["per_model"]["anthropic:haiku"]
        assert (v["primary_only"], v["shadow_only"]) == (1, 0), (
            "a HOLD that correctly sat out a non-move must score for "
            "the primary — otherwise any more-decisive model or prompt "
            "wins for free"
        )

    def test_a_decisive_shadow_cannot_farm_free_wins(self, tmp_path):
        """Six flat outcomes, shadow always takes a direction. Under the
        old scoring this was 6-0 shadow; it is really 0-6."""
        rows, preds = [], []
        for i, sym in enumerate("ABCDEF"):
            ts = f"2026-07-30 1{i}:00:00"
            rows.append((ts, "ensemble:pattern_recognizer", "anthropic",
                         "haiku", "SELL", 0, None, 0.001, 900,
                         '{"symbol": "%s", "verdict": "HOLD"}' % sym))
            preds.append((sym, ts.replace(" ", "T"), 0.3))
        db = _mk_profile(tmp_path, "403", rows, preds)
        v = collect_fleet_metrics([db])["per_model"]["anthropic:haiku"]
        assert v["shadow_only"] == 0
        assert v["primary_only"] == 6

    def test_set_level_rows_are_scored_per_symbol(self, tmp_path):
        """2026-08-23 (docs/25 item 1.3): an apex trade-set row is no
        longer "ungradable" — it is exploded symbol by symbol. Here the
        primary passed (no trades) while the shadow shorted CVX and
        GOOG. CVX fell 6% → the shadow's short was right and the
        primary's implicit HOLD missed a real move; GOOG has no resolved
        outcome → not scored, not credited to anyone. Symmetric by
        construction: both sides are graded against the same move."""
        db = _mk_profile(
            tmp_path, "404",
            shadow_rows=[(NOW, "batch_select", "openai", "nano",
                          "CVX:SHORT,GOOG:SHORT", 0, None, 0.001, 900,
                          '{"trades": []}')],
            predictions=[("CVX", "2026-07-30T14:02:00", -6.0)],
        )
        v = collect_fleet_metrics([db])["per_model"]["openai:nano"]
        assert v["ungradable"] == 0
        assert v["dis_resolved"] == 1
        assert (v["shadow_only"], v["primary_only"]) == (1, 0)
        assert (v["shadow_right"], v["primary_right"]) == (1, 0)
        assert v["edge_n"] == 1 and v["edge_points"] > 0

    def test_set_level_row_with_no_outcomes_is_pending(self, tmp_path):
        db = _mk_profile(
            tmp_path, "405",
            shadow_rows=[(NOW, "batch_select", "openai", "nano",
                          "CVX:BUY", 0, None, 0.001, 900,
                          '{"trades": [{"symbol": "GOOG", "action": "BUY"}]}')],
            predictions=[],
        )
        v = collect_fleet_metrics([db])["per_model"]["openai:nano"]
        assert v["dis_pending"] == 1
        assert v["dis_resolved"] == 0
        assert (v["shadow_right"], v["primary_right"]) == (0, 0)

    def test_set_level_symbol_both_picked_same_way_is_not_a_disagreement(
            self, tmp_path):
        """Both sides picked CVX:BUY; only the shadow added GOOG:BUY.
        CVX must NOT be scored (no disagreement on it); GOOG is a
        BUY-vs-HOLD disagreement scored against GOOG's move."""
        db = _mk_profile(
            tmp_path, "406",
            shadow_rows=[(NOW, "batch_select", "openai", "nano",
                          "CVX:BUY,GOOG:BUY", 0, None, 0.001, 900,
                          '{"trades": [{"symbol": "CVX", "action": "BUY"}]}')],
            predictions=[("CVX", "2026-07-30T14:02:00", 5.0),
                         ("GOOG", "2026-07-30T14:02:00", 4.0)],
        )
        v = collect_fleet_metrics([db])["per_model"]["openai:nano"]
        assert v["dis_resolved"] == 1          # GOOG only
        assert v["shadow_only"] == 1           # GOOG rose, shadow bought


class TestGateSpecialistsAreNotForecasters:
    """A VETO-authority specialist decides whether a trade HAPPENS; it
    does not predict direction. Scoring "VETO" as a bearish forecast
    against raw price was a category error that survived the first
    round of fixes and produced an apparent 31-2 result on decisions
    that never moved a dollar."""

    def test_moot_when_no_trade_was_taken(self, tmp_path):
        db = _mk_profile(
            tmp_path, "409",
            shadow_rows=[(NOW, "ensemble:adversarial_reviewer", "anthropic",
                          "haiku", "VETO", 0, None, 0.001, 900, PP_HOLD)],
            # predicted_signal defaults to HOLD below == no trade taken
            predictions=[("CVX", "2026-07-30T14:02:00", -6.0)],
        )
        v = collect_fleet_metrics([db])["per_model"]["anthropic:haiku"]
        assert v["moot"] == 1
        assert (v["shadow_only"], v["primary_only"]) == (0, 0), (
            "a gate verdict on a trade that was never opened cannot be "
            "right or wrong — crediting it manufactures a result"
        )

    def test_block_scores_when_a_taken_trade_lost(self, tmp_path):
        # System went LONG and lost 6%. The shadow's VETO was correct;
        # the primary's HOLD let a loser through.
        db = _mk_profile(
            tmp_path, "410",
            shadow_rows=[(NOW, "ensemble:adversarial_reviewer", "anthropic",
                          "haiku", "VETO", 0, None, 0.001, 900, PP_HOLD)],
            predictions=[("CVX", "2026-07-30T14:02:00", -6.0, "BUY")],
        )
        v = collect_fleet_metrics([db])["per_model"]["anthropic:haiku"]
        assert (v["shadow_only"], v["primary_only"], v["moot"]) == (1, 0, 0)

    def test_short_direction_flips_the_pnl(self, tmp_path):
        """Price ROSE 6% on a SHORT — the position lost, so the block
        was right even though the raw return is positive."""
        db = _mk_profile(
            tmp_path, "411",
            shadow_rows=[(NOW, "ensemble:adversarial_reviewer", "anthropic",
                          "haiku", "VETO", 0, None, 0.001, 900, PP_HOLD)],
            predictions=[("CVX", "2026-07-30T14:02:00", 6.0, "SHORT")],
        )
        v = collect_fleet_metrics([db])["per_model"]["anthropic:haiku"]
        assert (v["shadow_only"], v["primary_only"]) == (1, 0)

    def test_allow_scores_when_a_taken_trade_won(self, tmp_path):
        db = _mk_profile(
            tmp_path, "412",
            shadow_rows=[(NOW, "ensemble:adversarial_reviewer", "anthropic",
                          "haiku", "VETO", 0, None, 0.001, 900, PP_HOLD)],
            predictions=[("CVX", "2026-07-30T14:02:00", 6.0, "BUY")],
        )
        v = collect_fleet_metrics([db])["per_model"]["anthropic:haiku"]
        assert (v["primary_only"], v["shadow_only"]) == (1, 0)

    def test_flat_taken_trade_vindicates_nobody(self, tmp_path):
        db = _mk_profile(
            tmp_path, "413",
            shadow_rows=[(NOW, "ensemble:adversarial_reviewer", "anthropic",
                          "haiku", "VETO", 0, None, 0.001, 900, PP_HOLD)],
            predictions=[("CVX", "2026-07-30T14:02:00", 0.4, "BUY")],
        )
        v = collect_fleet_metrics([db])["per_model"]["anthropic:haiku"]
        assert v["ungradable"] == 1
        assert (v["shadow_only"], v["primary_only"]) == (0, 0)

    def test_sell_is_not_a_ruling_on_this_candidate(self, tmp_path):
        """A reviewer's SELL supports closing a DIFFERENT, existing
        position. Scoring it as a block on the reviewed candidate would
        invent a verdict the specialist never gave."""
        db = _mk_profile(
            tmp_path, "417",
            shadow_rows=[(NOW, "ensemble:adversarial_reviewer", "anthropic",
                          "haiku", "SELL", 0, None, 0.001, 900, PP_HOLD)],
            predictions=[("CVX", "2026-07-30T14:02:00", -6.0, "BUY")],
        )
        v = collect_fleet_metrics([db])["per_model"]["anthropic:haiku"]
        assert v["ungradable"] == 1
        assert (v["shadow_only"], v["primary_only"]) == (0, 0)

    def test_risk_assessor_is_also_a_gate(self, tmp_path):
        db = _mk_profile(
            tmp_path, "414",
            shadow_rows=[(NOW, "ensemble:risk_assessor", "anthropic",
                          "haiku", "VETO", 0, None, 0.001, 900, PP_HOLD)],
            predictions=[("CVX", "2026-07-30T14:02:00", -6.0)],
        )
        v = collect_fleet_metrics([db])["per_model"]["anthropic:haiku"]
        assert v["moot"] == 1

    def test_forecast_specialists_still_grade_on_price(self, tmp_path):
        """pattern_recognizer has no veto authority — its BUY/SELL IS a
        directional claim and is graded against the price move whether
        or not a trade was taken."""
        db = _mk_profile(
            tmp_path, "415",
            shadow_rows=[(NOW, "ensemble:pattern_recognizer", "anthropic",
                          "haiku", "BUY", 0, None, 0.001, 900, PP_SELL)],
            predictions=[("CVX", "2026-07-30T14:02:00", 6.0)],
        )
        v = collect_fleet_metrics([db])["per_model"]["anthropic:haiku"]
        assert v["moot"] == 0
        assert (v["shadow_only"], v["primary_only"]) == (1, 0)


class TestPageConsistency:
    """2026-08-07 — found by rendering the deployed page and reading it.
    The summary tables had drifted from the corrected scoring beneath
    them: a filesystem path in a column, gate verdicts filed under a
    directional heading, and a row whose parts didn't sum to its total.

    (Originally written 2026-07-30 but lost before commit when
    droplet-sync hard-reset the working tree; re-derived against the
    current implementation.)
    """

    def test_profile_label_is_not_a_filesystem_path(self, tmp_path):
        """The Profile column read '/opt/quantopsai/p212' — the label
        stripped the filename but left the directory attached."""
        db = _mk_profile(
            tmp_path, "418",
            shadow_rows=[(NOW, "ensemble:pattern_recognizer", "anthropic",
                          "haiku", "BUY", 0, None, 0.001, 900, PP_SELL)],
            predictions=[("CVX", "2026-07-30T14:02:00", 6.0)],
        )
        m = collect_fleet_metrics([db])
        label = m["recent_disagreements"][0]["profile"]
        assert label == "p418"
        assert "/" not in label and os.sep not in label

    def test_gate_verdicts_are_not_filed_under_bearish(self, tmp_path):
        """`stance()` maps VETO to bearish, so grouping by stance filed
        every reviewer veto under a directional heading — the same
        category error the scoring was fixed for."""
        db = _mk_profile(
            tmp_path, "419",
            shadow_rows=[(NOW, "ensemble:adversarial_reviewer", "anthropic",
                          "haiku", "HOLD", 0, None, 0.001, 900,
                          '{"symbol": "CVX", "verdict": "VETO"}')],
            predictions=[("CVX", "2026-07-30T14:02:00", -6.0, "BUY")],
        )
        actions = collect_fleet_metrics([db])["by_primary_action"]
        assert "gate: block" in actions
        assert "bearish" not in actions

    def test_gate_allow_is_its_own_row(self, tmp_path):
        db = _mk_profile(
            tmp_path, "420",
            shadow_rows=[(NOW, "ensemble:adversarial_reviewer", "anthropic",
                          "haiku", "VETO", 0, None, 0.001, 900,
                          '{"symbol": "CVX", "verdict": "HOLD"}')],
            predictions=[("CVX", "2026-07-30T14:02:00", -6.0, "BUY")],
        )
        actions = collect_fleet_metrics([db])["by_primary_action"]
        assert "gate: allow" in actions
        assert "neutral" not in actions

    def test_gate_exit_advice_gets_its_own_row(self, tmp_path):
        """A reviewer SELL is advice about a DIFFERENT held position. It
        must not be silently counted as a block on this candidate."""
        db = _mk_profile(
            tmp_path, "421",
            shadow_rows=[(NOW, "ensemble:risk_assessor", "anthropic",
                          "haiku", "HOLD", 0, None, 0.001, 900,
                          '{"symbol": "CVX", "verdict": "SELL"}')],
            predictions=[("CVX", "2026-07-30T14:02:00", -6.0, "BUY")],
        )
        actions = collect_fleet_metrics([db])["by_primary_action"]
        assert "gate: exit advice" in actions
        assert "gate: block" not in actions

    def test_forecast_verdicts_still_group_by_direction(self, tmp_path):
        db = _mk_profile(
            tmp_path, "422",
            shadow_rows=[(NOW, "ensemble:pattern_recognizer", "anthropic",
                          "haiku", "BUY", 0, None, 0.001, 900, PP_SELL)],
            predictions=[("CVX", "2026-07-30T14:02:00", 6.0)],
        )
        actions = collect_fleet_metrics([db])["by_primary_action"]
        assert "bearish" in actions
        assert not any(k.startswith("gate:") for k in actions)

    def test_resolved_disagreements_reconcile_exactly(self, tmp_path):
        """Every resolved row must land in exactly one outcome bucket.
        The per-model table showed Resolved=896 against buckets summing
        to 859 because `ungradable` had no column — numbers that don't
        add up train the reader to distrust the page."""
        rows, preds = [], []
        cases = [
            # (purpose, shadow verdict, primary verdict, return, traded)
            ("ensemble:pattern_recognizer", "BUY", "SELL", 6.0, "HOLD"),
            ("ensemble:pattern_recognizer", "BUY", "SELL", -6.0, "HOLD"),
            ("ensemble:pattern_recognizer", "BUY", "HOLD", 0.2, "HOLD"),
            ("ensemble:adversarial_reviewer", "VETO", "HOLD", -6.0, "HOLD"),
            ("ensemble:adversarial_reviewer", "VETO", "HOLD", -6.0, "BUY"),
            ("batch_select", "A:BUY", "HOLD", 6.0, "HOLD"),
        ]
        for i, (purpose, sv, pv, ret, traded) in enumerate(cases):
            ts = f"2026-07-30 1{i}:00:00"
            sym = f"SYM{i}"
            rows.append((ts, purpose, "anthropic", "haiku", sv, 0, None,
                         0.001, 900,
                         '{"symbol": "%s", "verdict": "%s"}' % (sym, pv)))
            preds.append((sym, ts.replace(" ", "T"), ret, traded))
        db = _mk_profile(tmp_path, "423", rows, preds)
        v = collect_fleet_metrics([db])["per_model"]["anthropic:haiku"]
        bucketed = (v["shadow_only"] + v["primary_only"] + v["both_right"]
                    + v["neither_right"] + v["moot"] + v["ungradable"])
        assert bucketed == v["dis_resolved"], (
            f"{v['dis_resolved']} resolved but {bucketed} bucketed — "
            f"a resolved row fell through every branch"
        )

    def test_template_does_not_truncate_the_variant_tag(self):
        """'gemini-3.1-flash-lite@adve' hid the one token that
        identifies a prompt-variant arm."""
        tpl = open(os.path.join(os.path.dirname(__file__), os.pardir,
                                "templates", "shadow.html")).read()
        for chop in ("[:26]", "[:22]", "[:20]"):
            assert chop not in tpl, f"model-name truncation came back: {chop}"

    def test_every_outcome_bucket_is_rendered(self):
        tpl = open(os.path.join(os.path.dirname(__file__), os.pardir,
                                "templates", "shadow.html")).read()
        for field in ("v.shadow_only", "v.primary_only", "v.both_right",
                      "v.neither_right", "v.moot", "v.ungradable"):
            assert field in tpl, f"outcome bucket not shown: {field}"


class TestEffectiveSampleSize:
    def test_repeat_reviews_of_one_name_are_one_unit(self, tmp_path):
        """Nine reviews of CVX resolving against one price move are
        nine rows but ONE piece of evidence."""
        rows, preds = [], []
        for i in range(9):
            ts = f"2026-07-30 1{i}:00:00"
            rows.append((ts, "ensemble:pattern_recognizer", "anthropic",
                         "haiku", "SELL", 0, None, 0.001, 900, PP_HOLD))
            preds.append(("CVX", ts.replace(" ", "T"), -6.0))
        db = _mk_profile(tmp_path, "405", rows, preds)
        v = collect_fleet_metrics([db])["per_model"]["anthropic:haiku"]
        assert v["dis_resolved"] == 9
        assert v["dis_units"] == 1, (
            "row count must not be mistaken for independent evidence"
        )

    def test_distinct_symbols_count_separately(self, tmp_path):
        rows, preds = [], []
        for i, sym in enumerate(("CVX", "TSLA", "GOOG")):
            ts = f"2026-07-30 1{i}:00:00"
            rows.append((ts, "ensemble:pattern_recognizer", "anthropic",
                         "haiku", "SELL", 0, None, 0.001, 900,
                         '{"symbol": "%s", "verdict": "HOLD"}' % sym))
            preds.append((sym, ts.replace(" ", "T"), -6.0))
        db = _mk_profile(tmp_path, "406", rows, preds)
        v = collect_fleet_metrics([db])["per_model"]["anthropic:haiku"]
        assert v["dis_units"] == 3


class TestSchemaDriftDegradesNotDisappears:
    def test_missing_predicted_signal_still_grades_forecasts(self, tmp_path):
        """An older profile DB without `predicted_signal` must not
        silently zero every outcome on the page — the failure mode this
        whole change exists to eliminate."""
        db = str(tmp_path / "quantopsai_profile_416.db")
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
                status TEXT, actual_return_pct REAL
            )""")   # no predicted_signal column
        conn.execute(
            "INSERT INTO ai_shadow_calls (timestamp, purpose, provider,"
            " model, parsed_signal, agreement, error, cost_usd,"
            " latency_ms, primary_parsed) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (NOW, "ensemble:pattern_recognizer", "anthropic", "haiku",
             "BUY", 0, None, 0.001, 900, PP_SELL))
        conn.execute(
            "INSERT INTO ai_predictions (symbol, timestamp, status,"
            " actual_return_pct) VALUES ('CVX','2026-07-30T14:02:00',"
            " 'resolved', 6.0)")
        conn.commit()
        conn.close()
        v = collect_fleet_metrics([db])["per_model"]["anthropic:haiku"]
        assert v["dis_resolved"] == 1
        assert v["shadow_only"] == 1


class TestMatchWindow:
    def test_window_is_wide_enough_for_cycle_jitter(self):
        assert shadow_metrics.MATCH_WINDOW_SEC == 1800.0

    def test_ten_minute_offset_now_resolves(self, tmp_path):
        """Ten minutes of jitter between the specialist call and the
        prediction write used to discard the outcome entirely."""
        db = _mk_profile(
            tmp_path, "407",
            shadow_rows=[(NOW, "ensemble:pattern_recognizer", "anthropic",
                          "haiku", "VETO", 0, None, 0.001, 900, PP_SELL)],
            predictions=[("CVX", "2026-07-30T14:10:00", -6.0)],
        )
        v = collect_fleet_metrics([db])["per_model"]["anthropic:haiku"]
        assert v["dis_resolved"] == 1 and v["dis_pending"] == 0

    def test_next_cycle_is_still_not_a_match(self, tmp_path):
        """The window must not stretch far enough to join a DIFFERENT
        trading cycle's prediction."""
        db = _mk_profile(
            tmp_path, "408",
            shadow_rows=[(NOW, "ensemble:pattern_recognizer", "anthropic",
                          "haiku", "VETO", 0, None, 0.001, 900, PP_SELL)],
            predictions=[("CVX", "2026-07-30T18:00:00", -6.0)],
        )
        v = collect_fleet_metrics([db])["per_model"]["anthropic:haiku"]
        assert v["dis_resolved"] == 0 and v["dis_pending"] == 1

    def test_daily_email_uses_the_same_window(self):
        """The email and the page must never disagree about which
        outcomes exist."""
        src = open(os.path.join(os.path.dirname(__file__), os.pardir,
                                "shadow_eval.py")).read()
        idx = src.index("def fetch_recently_resolved_disagreements")
        body = src[idx:idx + 3000]
        assert "MATCH_WINDOW_SEC" in body
        assert "<= 300 " not in body, "hardcoded 300s window came back"


class TestPromptVariantArm:
    def test_tag_splitting(self):
        assert prompt_variants.split_variant_tag(
            "gemini-3.1-flash-lite@adversarial_v2"
        ) == ("gemini-3.1-flash-lite", "adversarial_v2")
        assert prompt_variants.split_variant_tag(
            "gpt-4.1-nano") == ("gpt-4.1-nano", None)

    def test_rewrites_the_reviewer_prompt(self):
        from specialists.adversarial_reviewer import build_prompt
        base = build_prompt(
            [{"symbol": "CVX", "score": 60}, {"symbol": "TSLA", "score": 55}],
            ctx=None)
        out = prompt_variants.apply_variant(
            "adversarial_v2", "ensemble:adversarial_reviewer", base)
        assert out is not None and out != base
        # The evidence gate is the point of the variant.
        assert "EVIDENCE GATE" in out
        assert "NOT THIS TRADE'S ADVOCATE" in out
        # ensemble.py requires `confidence`; dropping it would make the
        # variant un-promotable.
        assert '"confidence"' in out
        # The candidate count must survive the rewrite intact.
        assert "exactly 2 entries" in out
        # Everything before the output contract (regime, book, checklist)
        # must be preserved verbatim — only the tail is replaced.
        assert out.startswith(base[:base.index("Return a STRICT JSON ARRAY")])

    def test_other_purposes_are_skipped_not_duplicated(self):
        assert prompt_variants.apply_variant(
            "adversarial_v2", "ensemble:risk_assessor", "anything") is None

    def test_unknown_tag_is_skipped(self):
        assert prompt_variants.apply_variant(
            "no_such_variant", "ensemble:adversarial_reviewer", "x") is None

    def test_fails_closed_when_the_base_prompt_changes(self):
        """A transform that can't find its anchor must SKIP, never send
        the unmodified prompt — that would run the primary model against
        itself and report a false null."""
        assert prompt_variants.apply_variant(
            "adversarial_v2", "ensemble:adversarial_reviewer",
            "a reviewer prompt that no longer has the output contract",
        ) is None

    def test_anchor_present_but_count_missing_also_skips(self):
        assert prompt_variants.apply_variant(
            "adversarial_v2", "ensemble:adversarial_reviewer",
            "Return a STRICT JSON ARRAY and then stop talking",
        ) is None


class TestVariantSurvivesTheSettingsForm:
    """The settings page rebuilds `shadow_models` from the posted
    checkboxes. A variant arm that has no checkbox of its own is not
    merely invisible — it is DELETED the first time the operator opens
    settings and hits save, silently ending the experiment."""

    def test_settings_template_renders_a_variant_checkbox(self):
        tpl = open(os.path.join(os.path.dirname(__file__), os.pardir,
                                "templates", "settings.html")).read()
        assert "shadow_variants" in tpl
        # Posted under the SAME name as ordinary shadow models, so the
        # existing save handler round-trips it with no changes.
        assert 'name="shadow_models" value="{{ _ventry }}"' in tpl
        # Built from the profile's own primary model — that is the
        # whole point of a prompt variant.
        assert "_prim_entry ~ '@' ~ _var.tag" in tpl

    def test_route_supplies_the_variant_catalogue(self):
        src = open(os.path.join(os.path.dirname(__file__), os.pardir,
                                "views.py")).read()
        assert "shadow_variants=shadow_variants" in src
        assert "describe_variants" in src

    def test_tagged_entry_survives_the_save_handler_filter(self):
        """The handler keeps only values containing ':'. A tagged entry
        must pass that filter intact, tag and all."""
        entry = "google:gemini-3.1-flash-lite@adversarial_v2"
        assert ":" in entry
        model, tag = prompt_variants.split_variant_tag(
            entry.partition(":")[2])
        assert (model, tag) == ("gemini-3.1-flash-lite", "adversarial_v2")

    def test_catalogue_shape(self):
        cat = prompt_variants.describe_variants()
        assert any(v["tag"] == "adversarial_v2" for v in cat)
        v = next(v for v in cat if v["tag"] == "adversarial_v2")
        assert v["label"] and v["description"]
        assert v["purposes"] == ["ensemble:adversarial_reviewer"]


class TestVariantDispatchWiring:
    def test_dispatch_applies_variants_and_labels_the_arm(self):
        src = open(os.path.join(os.path.dirname(__file__), os.pardir,
                                "shadow_eval.py")).read()
        idx = src.index("def dispatch_shadow_calls")
        body = src[idx:]
        assert "split_variant_tag" in body and "apply_variant" in body
        # The ledger must record the tagged label, or the variant arm
        # collides with the primary model's own row in /shadow.
        assert "model_label=model_label" in body
        # A variant that returns None must skip the call entirely.
        assert "if shadow_prompt is None:" in body
        # The variant prompt — not the primary's — is what gets sent.
        assert "prompt=shadow_prompt" in body

    def test_same_provider_arm_reuses_the_operational_key(self):
        """A gemini variant arm has no entry in the user's shadow key
        map; without this fallback every call would write a
        'no api key configured' row instead of running."""
        src = open(os.path.join(os.path.dirname(__file__), os.pardir,
                                "shadow_eval.py")).read()
        idx = src.index("def dispatch_shadow_calls")
        body = src[idx:]
        assert "provider == primary_provider" in body
        assert "_working_key_for_provider" in body
