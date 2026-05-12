"""option_spread_risk specialist gets book Greeks + budget caps
in its prompt context (2026-05-12).

Pre-this-commit, option_spread_risk only saw the proposal candidate.
Couldn't reason about "this trade pushes the book past
max_short_vega_dollars" because it didn't know the book's current
net_vega or the cap. Now the prompt surfaces:
  - Current book Greeks (delta/gamma/vega/theta) — only when the
    book actually has option positions.
  - Per-profile Greek-budget caps (delta_pct, theta burn, short
    vega) — only when set on ctx.

Failure-tolerant: if the broker call fails or compute_book_greeks
raises, the prompt still renders without the Greeks line. The
specialist degrades gracefully to its pre-this-commit behavior.

This file pins:
- GREEKS LINE: appears when n_options_legs > 0; omitted when book
  has no options (no signal to surface).
- BUDGET CAPS: appear when ctx has the cap attributes set; omitted
  when None.
- FAILURE: broker exception → prompt renders without Greeks line
  (no crash).
- ATTRIBUTION: caps render as percentages for *_pct attrs and as
  dollars for the others.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from specialists import option_spread_risk


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ctx(**overrides):
    base = {
        "max_per_trade_loss": 500.0,
        "max_net_options_delta_pct": 0.05,
        "max_theta_burn_dollars_per_day": 50.0,
        "max_short_vega_dollars": 500.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _candidate():
    return {
        "symbol": "CWAN",
        "iv_rank": 65,
        "dte": 32,
        "spread_max_loss": 230,
    }


# ---------------------------------------------------------------------------
# Greeks line surfacing
# ---------------------------------------------------------------------------

class TestGreeksContextLine:
    def test_greeks_line_appears_when_book_has_options(self):
        with patch(
            "specialists.option_spread_risk._current_positions",
            return_value=[
                {"symbol": "AAPL", "qty": 100, "current_price": 150.0},
            ],
        ), patch(
            "pipelines.risk.compute_book_greeks",
            return_value={
                "n_options_legs": 2,
                "net_delta": 35.0, "net_gamma": 0.12,
                "net_vega": -200.0, "net_theta": -45.0,
            },
        ):
            prompt = option_spread_risk.build_prompt(
                [_candidate()], _ctx(),
            )
        assert "Current book Greeks" in prompt
        assert "net_delta" in prompt
        assert "net_vega" in prompt
        assert "options_legs=2" in prompt

    def test_greeks_line_omitted_when_no_options_legs(self):
        """Stock-only book: nothing for the option specialist to
        say about Greeks. Don't pollute the prompt."""
        with patch(
            "specialists.option_spread_risk._current_positions",
            return_value=[
                {"symbol": "AAPL", "qty": 100, "current_price": 150.0},
            ],
        ), patch(
            "pipelines.risk.compute_book_greeks",
            return_value={
                "n_options_legs": 0,
                "net_delta": 0.0, "net_gamma": 0.0,
                "net_vega": 0.0, "net_theta": 0.0,
            },
        ):
            prompt = option_spread_risk.build_prompt(
                [_candidate()], _ctx(),
            )
        assert "Current book Greeks" not in prompt

    def test_greeks_line_omitted_when_positions_fail(self):
        """Broker call raises → prompt renders without the
        Greeks line. Specialist still functional."""
        with patch(
            "specialists.option_spread_risk._current_positions",
            side_effect=RuntimeError("broker down"),
        ):
            prompt = option_spread_risk.build_prompt(
                [_candidate()], _ctx(),
            )
        assert "Current book Greeks" not in prompt
        # Sanity: rest of prompt still there
        assert "option-specific risk specialist" in prompt
        assert "MAX-LOSS" in prompt

    def test_greeks_line_omitted_when_compute_fails(self):
        """compute_book_greeks raises → graceful degradation."""
        with patch(
            "specialists.option_spread_risk._current_positions",
            return_value=[
                {"symbol": "AAPL", "qty": 100, "current_price": 150.0},
            ],
        ), patch(
            "pipelines.risk.compute_book_greeks",
            side_effect=RuntimeError("greek compute failed"),
        ):
            prompt = option_spread_risk.build_prompt(
                [_candidate()], _ctx(),
            )
        assert "Current book Greeks" not in prompt


# ---------------------------------------------------------------------------
# Budget caps surfacing
# ---------------------------------------------------------------------------

class TestBudgetCapsLines:
    def test_caps_appear_when_set(self):
        prompt = option_spread_risk.build_prompt(
            [_candidate()], _ctx(),
        )
        # The per-profile caps block exists
        assert "Per-profile Greek-budget caps" in prompt
        # delta_pct rendered as percentage
        assert "5.0%" in prompt
        # theta and vega rendered as dollars
        assert "$50" in prompt
        assert "$500" in prompt

    def test_caps_omitted_when_all_none(self):
        ctx = _ctx(
            max_net_options_delta_pct=None,
            max_theta_burn_dollars_per_day=None,
            max_short_vega_dollars=None,
        )
        prompt = option_spread_risk.build_prompt(
            [_candidate()], ctx,
        )
        assert "Per-profile Greek-budget caps" not in prompt

    def test_partial_caps_still_show_set_ones(self):
        ctx = _ctx(
            max_net_options_delta_pct=0.07,
            max_theta_burn_dollars_per_day=None,
            max_short_vega_dollars=None,
        )
        prompt = option_spread_risk.build_prompt(
            [_candidate()], ctx,
        )
        # Caps block still present (one cap is set)
        assert "Per-profile Greek-budget caps" in prompt
        assert "7.0%" in prompt
        # Theta/vega caps NOT in the caps block
        # (They might appear elsewhere in the prompt template,
        # so just check they're absent from the caps block.)
        caps_section = prompt.split("Per-profile Greek-budget caps:")[1].split(
            "For each candidate"
        )[0]
        assert "$theta" not in caps_section
        assert "$vega" not in caps_section


# ---------------------------------------------------------------------------
# Existing prompt scaffolding still intact (regression check)
# ---------------------------------------------------------------------------

class TestExistingPromptStructure:
    def test_four_veto_classes_still_listed(self):
        """Pre-existing veto criteria must remain in the prompt
        regardless of the new context lines."""
        prompt = option_spread_risk.build_prompt(
            [_candidate()], _ctx(),
        )
        assert "MAX-LOSS" in prompt
        assert "IV CRUSH" in prompt
        assert "GAMMA RISK" in prompt
        assert "CREDIT/MAX-LOSS ratio" in prompt

    def test_verdict_discipline_block_still_present(self):
        prompt = option_spread_risk.build_prompt(
            [_candidate()], _ctx(),
        )
        assert "VERDICT DISCIPLINE" in prompt
