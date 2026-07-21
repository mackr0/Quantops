"""2026-07-21 (evening) — the POST-AI meta suppressor becomes a
trimmer, mirror of the morning's meta-pregate fix.

The pregate got the backstop cap ("can never empty a live cycle") but
STEP 4.5's post-AI suppression ran one step later with the SAME
starving model and NO cap — fleet trade_drops show it suppressing
every AI-selected entry at 1–26% edge vs the 30% threshold (p211
VALE+GE in one second, p214 IREN+LLY, p219 LLY on repeat), re-zeroing
the cycles the pregate was forbidden to zero.

Pins (the REAL decision function, _meta_suppression_plan):
  1. all-below-threshold → suppress at most half; least-bad spared
  2. a single-trade selection can never be suppressed (1//2 == 0)
  3. exits are never suppressible (entry-edge model must not strand a
     live position — the stranded-GOOG class, one gate over)
  4. within-cap suppression unchanged; exact-cap boundary unchanged
  5. unscoreable (None) meta_prob is never suppressible (fail-open)
  6. call site: spared trades keep ORIGINAL confidence (no blend) —
     structural, because blending sub-threshold meta_prob hands the
     same knife to the confidence gate (the AMT 44-vs-45 shape)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

THRESHOLD = 0.30


def _t(sym, action="BUY", conf=60):
    return {"symbol": sym, "action": action, "confidence": conf}


class TestSuppressionPlan:

    def test_all_below_suppresses_at_most_half(self):
        from trade_pipeline import _meta_suppression_plan
        trades = [_t("A"), _t("B"), _t("C"), _t("D")]
        scored = list(zip(trades, [0.10, 0.20, 0.05, 0.15]))
        below, spared = _meta_suppression_plan(scored, THRESHOLD)
        assert len(below) == 2
        assert {t["symbol"] for _mp, t in below} == {"C", "A"}, (
            "the cap must keep the WORST by meta_prob")
        assert {id(t) for t in (trades[1], trades[3])} == spared

    def test_single_trade_never_suppressed(self):
        from trade_pipeline import _meta_suppression_plan
        trades = [_t("MS")]
        below, spared = _meta_suppression_plan(
            list(zip(trades, [0.03])), THRESHOLD)
        assert below == []
        assert spared == {id(trades[0])}

    def test_exits_never_suppressible(self):
        from trade_pipeline import _meta_suppression_plan
        trades = [_t("A", action="SELL"), _t("B", action="STRONG_SELL"),
                  _t("C", action="MULTILEG_CLOSE"), _t("D", action="BUY")]
        scored = list(zip(trades, [0.01, 0.01, 0.01, 0.01]))
        below, spared = _meta_suppression_plan(scored, THRESHOLD)
        assert {t["symbol"] for _mp, t in below} == {"D"}, (
            "only the entry may be suppressed; an entry-edge model must "
            "never veto an exit")

    def test_within_cap_unchanged(self):
        from trade_pipeline import _meta_suppression_plan
        trades = [_t("A"), _t("B"), _t("C"), _t("D")]
        scored = list(zip(trades, [0.10, 0.50, 0.60, 0.70]))
        below, spared = _meta_suppression_plan(scored, THRESHOLD)
        assert {t["symbol"] for _mp, t in below} == {"A"}
        assert spared == set()

    def test_exact_cap_boundary(self):
        from trade_pipeline import _meta_suppression_plan
        trades = [_t("A"), _t("B"), _t("C"), _t("D")]
        scored = list(zip(trades, [0.10, 0.20, 0.60, 0.70]))
        below, spared = _meta_suppression_plan(scored, THRESHOLD)
        assert {t["symbol"] for _mp, t in below} == {"A", "B"}
        assert spared == set()

    def test_none_meta_prob_not_suppressible(self):
        from trade_pipeline import _meta_suppression_plan
        trades = [_t("A"), _t("B")]
        below, spared = _meta_suppression_plan(
            list(zip(trades, [None, 0.05])), THRESHOLD)
        assert {t["symbol"] for _mp, t in below} == {"B"}


class TestCallSiteStructure:

    def test_spared_branch_keeps_original_confidence(self):
        """The spared branch must append WITHOUT calling
        adjust_confidence — blending sub-threshold meta_prob halves the
        confidence and re-kills the trade at the confidence gate."""
        src = open(os.path.join(REPO, "trade_pipeline.py")).read()
        idx = src.index("if id(t) in _spared_ids:")
        branch = src[idx:idx + 1000]
        cont = branch.index("continue")
        assert "adjust_confidence" not in branch[:cont], (
            "the spared branch blends confidence — the backstop cap is "
            "defeated one gate later")
        assert "filtered_trades.append(t)" in branch[:cont]

    def test_step45_uses_the_plan_function(self):
        """STEP 4.5 must route its suppression decision through
        _meta_suppression_plan so these tests exercise the real gate."""
        src = open(os.path.join(REPO, "trade_pipeline.py")).read()
        scoring = src.index("_meta_scored.append((t, meta_prob, online_prob))")
        window = src[scoring:scoring + 800]
        assert "_meta_suppression_plan(" in window
