"""The /shadow "bottom line" verdict — 2026-07-30.

Operator asked for an indicator that says, clearly and simply, which
arm is objectively better: more wins, more profit, less loss. The trap
in that request is that a page which always names a winner will name
one from noise — which is exactly what /shadow did before today (a
"27-5 rout" that was pure measurement artifact).

So the verdict is built to REFUSE. It scores each arm by the return
points you'd have banked by following it instead of the primary, and
only names a winner when the sample is big enough AND the edge is
unlikely under resampling. "Not enough evidence" is the default state.

The other thing pinned here: win COUNT and money can disagree. An arm
that loses most decisions can be ahead on points by catching the few
large moves. The verdict follows the money, because that is what the
operator asked about; the win rate is reported beside it so a
lopsided-but-lucky split stays visible.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import shadow_metrics as sm  # noqa: E402
from shadow_metrics import (  # noqa: E402
    _finalize_edge, decision_value, gate_value,
)


def _units(deltas):
    """Build the {unit_key: [deltas]} shape _finalize_edge consumes."""
    return {("p1", f"SYM{i}"): [d] for i, d in enumerate(deltas)}


class TestDecisionValue:
    def test_forecast_value_is_the_move_you_would_have_banked(self):
        assert decision_value("bullish", 4.0) == 4.0
        assert decision_value("bearish", 4.0) == -4.0
        assert decision_value("bearish", -4.0) == 4.0
        # Sitting out banks nothing — it does not bank the inverse.
        assert decision_value("neutral", -9.0) == 0.0
        assert decision_value(None, 4.0) is None

    def test_gate_value_blocking_banks_nothing(self):
        assert gate_value("block", -6.0) == 0.0
        assert gate_value("block", 6.0) == 0.0
        assert gate_value("allow", -6.0) == -6.0
        assert gate_value("allow", 6.0) == 6.0
        assert gate_value(None, 6.0) is None


class TestVerdictRefusesWithoutEvidence:
    def test_no_data_is_not_a_tie(self):
        out = _finalize_edge({})
        assert out["verdict"] == "insufficient"
        assert out["edge_points"] is None

    def test_small_but_lopsided_sample_gets_no_verdict(self):
        """Ten straight wins is exactly the shape that fooled us before.
        It must NOT produce a winner."""
        out = _finalize_edge(_units([3.0] * 10))
        assert out["verdict"] == "insufficient"
        assert "need at least" in out["verdict_line"]

    def test_large_sample_with_no_real_edge_gets_no_verdict(self):
        deltas = [2.0, -2.0] * 30       # 60 decisions, mean zero
        out = _finalize_edge(_units(deltas))
        assert out["verdict"] == "insufficient"
        assert out["edge_n"] == 60

    def test_repeat_reviews_of_one_symbol_are_one_decision(self):
        """Nine reviews of CVX resolving against one move must not
        become nine independent bets."""
        out = _finalize_edge({("p1", "CVX"): [4.0] * 9})
        assert out["edge_n"] == 1
        assert out["edge_points"] == 4.0


class TestVerdictCallsAClearWinner:
    def test_consistent_shadow_edge_is_named(self):
        out = _finalize_edge(_units([2.5] * 40))
        assert out["verdict"] == "shadow_better"
        assert out["verdict_leader"] == "shadow"
        assert out["edge_per_decision"] == 2.5
        assert "ahead" in out["verdict_line"]

    def test_consistent_primary_edge_is_named(self):
        out = _finalize_edge(_units([-2.5] * 40))
        assert out["verdict"] == "primary_better"
        assert out["verdict_leader"] == "primary"
        assert "Primary is ahead" in out["verdict_line"]


class TestMoneyBeatsWinCount:
    def test_losing_most_decisions_but_winning_on_points(self):
        """25 small losses, 15 large wins — behind on count, well ahead
        on money. The verdict must follow the money."""
        deltas = [-0.5] * 25 + [6.0] * 15
        out = _finalize_edge(_units(deltas))
        assert out["edge_wins"] == 15 and out["edge_losses"] == 25
        assert out["edge_points"] > 0
        assert out["verdict"] == "shadow_better", (
            "an arm ahead on P&L must not be scored as losing merely "
            "because it wins less often"
        )

    def test_win_rate_is_still_reported_for_that_case(self):
        out = _finalize_edge(_units([-0.5] * 25 + [6.0] * 15))
        assert out["edge_p_sign"] is not None
        # The sign test disagrees with the money test here — both
        # numbers must survive to the page so the split is visible.
        assert out["edge_p_sign"] != out["edge_p"]

    def test_one_freak_outlier_does_not_manufacture_a_verdict(self):
        """39 flat decisions and a single +200 point moonshot. The mean
        is positive but the evidence is one row — resampling must
        refuse."""
        out = _finalize_edge(_units([0.0] * 39 + [200.0]))
        assert out["edge_points"] > 0
        assert out["verdict"] == "insufficient"


class TestDeterminism:
    def test_same_data_gives_the_same_verdict_every_time(self):
        """A page that shuffles its own conclusion between refreshes is
        worse than no page."""
        units = _units([1.5, -0.5] * 20 + [3.0] * 10)
        runs = [_finalize_edge(units)["edge_p"] for _ in range(3)]
        assert len(set(runs)) == 1

    def test_bootstrap_skipped_below_the_verdict_threshold(self):
        assert sm._bootstrap_p([1.0] * (sm.MIN_DECISIONS_FOR_VERDICT - 1)) is None
        assert sm._bootstrap_p([1.0] * sm.MIN_DECISIONS_FOR_VERDICT) is not None


class TestSignTest:
    def test_fair_split_is_not_significant(self):
        assert sm._sign_test_p(20, 20) > 0.05

    def test_lopsided_split_is(self):
        assert sm._sign_test_p(38, 2) < 0.001

    def test_empty(self):
        assert sm._sign_test_p(0, 0) is None

    def test_large_n_uses_the_approximation_without_blowing_up(self):
        p = sm._sign_test_p(1600, 1400)
        assert p is not None and 0.0 <= p <= 1.0


class TestPageWiring:
    def test_template_leads_with_the_verdict(self):
        tpl = open(os.path.join(os.path.dirname(__file__), os.pardir,
                                "templates", "shadow.html")).read()
        assert "Bottom line" in tpl
        assert "verdict_line" in tpl
        assert "edge_per_decision" in tpl
        # The refusal state must be rendered, not just the winner ones.
        assert "Not enough evidence" in tpl
        # Cost has to sit next to the edge or "better" can't be judged.
        assert "Cost (30d)" in tpl
        # Bottom line must appear before the per-model detail table.
        assert tpl.index("Bottom line") < tpl.index("Per-model summary")

    def test_threshold_is_exposed_to_the_template(self):
        import glob
        m = sm.collect_fleet_metrics(
            sorted(glob.glob("/opt/quantopsai/quantopsai_profile_*.db"))[:1])
        assert m["verdict_min_decisions"] == sm.MIN_DECISIONS_FOR_VERDICT


class TestEvidenceFunnelRendered:
    """2026-08-03 — the operator saw '27,649 calls · 19,926 graded' in
    the header and 'Not enough evidence' in the verdict column and
    reasonably asked whether the evidence was being ignored. The card
    now renders the LIVE per-arm funnel (calls → graded → disagreements
    → deduplicated positions → moot vetoes → scored) plus an explicit
    checklist of the two verdict requirements, so the gap between
    inventory and proof is visible on the page itself."""

    def _render(self, edge_n, edge_p, moot=330):
        import jinja2
        tpl_dir = os.path.join(os.path.dirname(__file__), os.pardir,
                               "templates")
        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(tpl_dir), autoescape=True)
        src = open(os.path.join(tpl_dir, "shadow.html")).read()
        # Render the content block standalone (base.html carries app
        # context we don't need for a card-copy test).
        body = src.split("{% block content %}", 1)[1]
        body = body.rsplit("{% endblock %}", 1)[0]
        arm = {
            "calls": 13685, "graded": 9897, "disagree": 1240,
            "dis_units": 185, "moot": moot, "edge_n": edge_n,
            "edge_p": edge_p, "edge_p_sign": None,
            "edge_per_decision": 0.012, "edge_points": 0.9,
            "edge_wins": 36, "edge_losses": 35, "edge_ties": 7,
            "verdict": "insufficient", "verdict_leader": None,
            "verdict_line": "Not enough evidence — test line",
            "cost": 1.23,
            "errors": 0, "quota": 0, "throttled": 0,
            "latency_ms": 0, "latency_n": 0, "agree": 8000,
            "dis_resolved": 100, "shadow_right": 1, "primary_right": 1,
            "shadow_only": 1, "primary_only": 1, "both_right": 1,
            "neither_right": 1, "ungradable": 0, "dis_pending": 0,
        }
        m = {
            "overview": {"calls": 13685, "graded": 9897, "cost": 1.23,
                         "profiles": 1, "since_days": 30},
            "per_model": {"test:arm": arm},
            "by_purpose": {}, "by_primary_action": {},
            "daily": {}, "recent_disagreements": [],
            "verdict_min_decisions": 30,
        }
        return env.from_string(body).render(m=m)

    def test_funnel_numbers_and_moot_rendered(self):
        html = self._render(edge_n=78, edge_p=0.99)
        assert "Evidence funnel" in html
        assert "13,685" in html and "9,897" in html
        assert "185 distinct positions" in html
        assert "330" in html and "never traded" in html
        assert "78 scored against real" in html

    def test_unmet_p_requirement_shows_cross(self):
        html = self._render(edge_n=78, edge_p=0.99)
        # n met (78 >= 30), p unmet
        assert "&#10003;" in html or "✓" in html
        assert "needs &lt; 0.05" in html or "needs < 0.05" in html

    def test_unmet_n_requirement_shows_progress(self):
        html = self._render(edge_n=12, edge_p=None)
        assert "have 12" in html
        assert "no scored edge yet" in html

    def test_both_met_shows_double_check(self):
        html = self._render(edge_n=78, edge_p=0.01)
        assert html.count("✓") == 2 or html.count("&#10003;") == 2
