"""Specialists must SEE the alt-data signals they're asked to vote on
(#175, 2026-05-17).

Before this fix, format_candidate_brief stripped alt-data entirely.
sentiment_narrative was asked "are insiders buying?" without ever
being shown the insider data. format_candidate_for_specialist now
routes a per-specialist subset of alt-data into each specialist's
candidate render.

Pins the contract so a future refactor can't silently re-bifurcate.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


def _cand(symbol="AAPL", alt_data=None):
    return {
        "symbol": symbol,
        "signal": "BUY",
        "price": 100.0,
        "reason": "test",
        "alt_data": alt_data or {},
    }


class TestPerSpecialistRouting:
    """Each specialist gets its own alt-data subset."""

    def test_sentiment_specialist_sees_insider_data(self):
        from specialists._common import format_candidate_for_specialist
        c = _cand(alt_data={
            "insider": {"has_data": True, "recent_buys": 3, "recent_sells": 0},
            # Risk-side fields the sentiment specialist should NOT see
            "risk_factor_diff": {"has_new_risks": True, "added_risk_count": 5},
            "fda_inspections": {"has_data": True,
                                  "recent_citations_count": 2,
                                  "fda_name": "Pfizer"},
        })
        out = format_candidate_for_specialist(c, "sentiment_narrative")
        assert "insider" in out  # sentiment SEES insider
        assert "risk" not in out.lower()
        assert "FDA" not in out

    def test_risk_assessor_sees_fda_and_risk_factor_diff(self):
        from specialists._common import format_candidate_for_specialist
        c = _cand(alt_data={
            "fda_inspections": {"has_data": True,
                                  "recent_citations_count": 2,
                                  "fda_name": "Pfizer"},
            "risk_factor_diff": {"has_new_risks": True,
                                   "added_risk_count": 4},
            # Sentiment-side fields the risk specialist should NOT see
            "insider": {"has_data": True, "recent_buys": 3,
                        "recent_sells": 0},
        })
        out = format_candidate_for_specialist(c, "risk_assessor")
        assert "FDA" in out
        assert "newRisks" in out
        assert "insider(" not in out  # not on risk_assessor's route

    def test_adversarial_reviewer_sees_8k_events(self):
        """The adversarial reviewer focuses on downside catalysts —
        8-K item codes for bankruptcy, restatement, officer change
        must reach it."""
        from specialists._common import format_candidate_for_specialist
        c = _cand(alt_data={
            "recent_8k_events": {
                "events": [{
                    "date": "2026-05-17",
                    "items": ["1.03"],
                    "item_tags": ["bankruptcy"],
                }],
                "count": 1, "high_signal_count": 1,
            },
        })
        out = format_candidate_for_specialist(c, "adversarial_reviewer")
        assert "8K" in out
        assert "bankruptcy" in out

    def test_pattern_recognizer_sees_intraday_and_options(self):
        from specialists._common import format_candidate_for_specialist
        c = _cand(alt_data={
            "intraday": {"has_data": True, "pattern": "breakout"},
            "options": {"has_data": True, "iv_rank": 75.0},
            # Sentiment fields should NOT be on pattern_recognizer's route
            "stocktwits_sentiment": {"message_count_7d": 100,
                                      "net_sentiment_7d": 0.5},
        })
        out = format_candidate_for_specialist(c, "pattern_recognizer")
        assert "intraday(breakout)" in out
        assert "opts" in out
        assert "twits" not in out  # not on pattern_recognizer's route

    def test_earnings_analyst_sees_earnings_surprise(self):
        from specialists._common import format_candidate_for_specialist
        c = _cand(alt_data={
            "earnings_surprise": {"has_data": True, "surprise_pct": 12.5},
            "biotech_milestones": {},
        })
        out = format_candidate_for_specialist(c, "earnings_analyst")
        assert "earnSurp" in out

    def test_iv_skew_sees_macro_subkeys(self):
        from specialists._common import format_candidate_for_specialist
        c = _cand(alt_data={
            "macro": {
                "cboe_skew": {"skew_signal": "elevated"},
                "cross_asset_vol": {
                    "move": {"p30d_label": "extreme"},
                    "ovx": {"p30d_label": "normal"},
                    "gvz": {"p30d_label": "low"},
                },
            },
        })
        out = format_candidate_for_specialist(c, "iv_skew_specialist")
        assert "skew=elevated" in out
        assert "move=extreme" in out


class TestGracefulFallback:
    def test_unknown_specialist_falls_back_to_brief(self):
        """A specialist name not in the routing table → bare brief."""
        from specialists._common import format_candidate_for_specialist
        c = _cand(alt_data={"insider": {"has_data": True,
                                          "recent_buys": 5,
                                          "recent_sells": 0}})
        out = format_candidate_for_specialist(c, "made_up_specialist")
        # Bare brief — no alt-data rendered
        assert "insider" not in out
        assert "AAPL" in out

    def test_no_alt_data_falls_back_to_brief(self):
        from specialists._common import format_candidate_for_specialist
        c = _cand(alt_data={})
        out = format_candidate_for_specialist(c, "sentiment_narrative")
        assert "|" not in out  # no alt-data divider
        assert "AAPL" in out

    def test_alt_data_with_no_signal_falls_back_clean(self):
        """Every alt-data block returns empty-dict / has_data=False —
        the render shouldn't have a trailing `|` divider."""
        from specialists._common import format_candidate_for_specialist
        c = _cand(alt_data={
            "insider": {"has_data": False},
            "stocktwits_sentiment": {"message_count_7d": 0},
        })
        out = format_candidate_for_specialist(c, "sentiment_narrative")
        assert "|" not in out


class TestBackwardsCompat:
    """candidates_block called without specialist_name still works the
    old way (used by anything outside the ensemble path)."""

    def test_candidates_block_without_name_is_brief(self):
        from specialists._common import candidates_block
        c = _cand(alt_data={"insider": {"has_data": True,
                                          "recent_buys": 10,
                                          "recent_sells": 0}})
        out = candidates_block([c])
        # No specialist name → no alt-data
        assert "insider" not in out
        assert "AAPL" in out


class TestEnsembleIntegration:
    """Every specialist's build_prompt actually passes its NAME to
    candidates_block. Without this the rendering layer is wired but
    the specialists never use it."""

    def test_all_8_specialists_pass_their_name(self):
        import inspect
        from specialists import (
            adversarial_reviewer, earnings_analyst,
            pattern_recognizer, risk_assessor, sentiment_narrative,
            iv_skew_specialist, gamma_pin_specialist,
            option_spread_risk,
        )
        for mod in (adversarial_reviewer, earnings_analyst,
                    pattern_recognizer, risk_assessor,
                    sentiment_narrative, iv_skew_specialist,
                    gamma_pin_specialist, option_spread_risk):
            src = inspect.getsource(mod.build_prompt)
            assert f'specialist_name="{mod.NAME}"' in src, (
                f"{mod.NAME}.build_prompt does not pass its name to "
                "candidates_block — its specialist will see only the "
                "bare brief, not its alt-data subset"
            )
