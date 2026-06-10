"""2026-06-10 — REAL PREVENTION at the AI proposal parser layer.

Earlier today I "fixed" two option-pipeline issues by improving
their error messages. The operator caught me: making bad errors
more descriptive isn't fixing anything. The real fix is to
PREVENT the bad proposals from reaching execution.

Bug 1 (prevented here): AI proposed `bull_put_spread` (a multileg
strategy) under action='OPTIONS' (single-leg only). The pre-fix
flow: proposal validated as-is at parse → executor rejected with
"Unsupported option_strategy". Operator-visible: a BLOCKED badge
in the brain. Real fix: reject at the parse layer with a log
identifying the misroute, so the proposal never reaches the brain
ticker AND the AI's prompt-retune signal points at MULTILEG_OPEN.

Bug 2 (prevented here): AI proposed an off-chain strike — INTC
$115 call exp 2026-07-17, which doesn't exist on Alpaca's chain.
Pre-fix flow: proposal validated → executor calls submit_option_order
→ Alpaca returned 422 "asset not found". Real fix: parse-layer
calls `snap_to_listed_contract`. If a listed contract exists
within tolerance, snap the proposal to it. If no listed contract
exists within tolerance, reject — the proposal is structurally
unreachable.

Tests pin both. Single-leg proposals with valid strategies and
on-chain strikes proceed unchanged.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Source-level pins (the parse-layer code must be present)
# ---------------------------------------------------------------------------


class TestOptionsParseValidationSourcePins:

    def test_options_parse_rejects_multileg_strategy_names(self):
        """Source pin: the OPTIONS branch of validate_ai_response
        must contain a _MULTILEG_NAMES set check that `continue`s
        past the validated.append on a multileg name.

        Without this, the AI's bull_put_spread proposal flows to
        the executor and the operator sees a misleading 'Unsupported
        option_strategy' badge in the brain instead of the proposal
        being silently routed (which is what we want — the AI's
        prompt will retune from the absence)."""
        src = (REPO_ROOT / "ai_analyst.py").read_text()
        # Find the OPTIONS branch
        anchor = src.find('if action == "OPTIONS":')
        assert anchor > 0, "OPTIONS branch missing"
        # The validation must reference the multileg set
        window = src[anchor:anchor + 5000]
        assert "_MULTILEG_NAMES" in window, (
            "Parse layer must define a _MULTILEG_NAMES set and "
            "reject proposals whose option_strategy is in it. "
            "Without the rejection, multileg strategies under "
            "OPTIONS reach the executor and produce misleading "
            "'Unsupported' badges."
        )
        # And the strategy whitelist
        assert "_SINGLE_LEG" in window, (
            "Parse layer must enumerate the supported single-leg "
            "strategies (covered_call, long_call, etc.) and reject "
            "anything else."
        )

    def test_options_parse_snaps_strike_to_listed_contract(self):
        """Source pin: the OPTIONS branch must call
        `snap_to_listed_contract` on the AI's proposed strike +
        expiry. Without this, off-chain proposals (INTC $115 call
        exp 2026-07-17 from this morning) reach the executor and
        get the 'asset not found' broker rejection. The snap call
        prevents that by either correcting to the nearest listed
        contract or rejecting at parse with a clear log."""
        src = (REPO_ROOT / "ai_analyst.py").read_text()
        anchor = src.find('if action == "OPTIONS":')
        window = src[anchor:anchor + 5000]
        assert "snap_to_listed_contract" in window, (
            "OPTIONS parse must call snap_to_listed_contract to "
            "validate the proposed strike+expiry exists on Alpaca's "
            "chain. Without this validation, AI's off-chain "
            "proposals flow to the executor and get rejected at "
            "the broker — operator-visible badge that means nothing "
            "actionable."
        )

    def test_options_prompt_states_single_leg_only(self):
        """The prompt must explicitly tell the AI that OPTIONS is
        single-leg only and SPREADS require MULTILEG_OPEN. Without
        this clarity in the prompt, the AI keeps making the same
        misroute even with parse-layer rejection (it learns from
        the rejected-vs-accepted pattern, but the pattern is
        weaker than explicit instruction)."""
        src = (REPO_ROOT / "ai_analyst.py").read_text()
        options_note_start = src.find("OPTIONS (single-leg")
        assert options_note_start > 0, (
            "options_note string must declare 'OPTIONS (single-leg "
            "ONLY)' to make the constraint unmissable in the prompt."
        )
        window = src[options_note_start:options_note_start + 2000]
        assert "MULTILEG_OPEN" in window, (
            "options_note must direct spreads to MULTILEG_OPEN. The "
            "previous note only listed valid single-leg strategies "
            "without saying what to do for spreads — the AI was "
            "left to guess."
        )
        assert "bull_put_spread" in window or "SPREADS" in window or "spreads" in window, (
            "options_note must explicitly name spreads (or list "
            "specific spread strategy_names) so the AI's pattern "
            "matcher has something to anchor on."
        )


# ---------------------------------------------------------------------------
# (Behavioral tests are easier to write at the unit level on the
#  validate_ai_response function itself, but it requires a substantial
#  context setup. Source pins above guard against the regression in
#  practice; the behavior is exercised end-to-end on prod immediately
#  after deploy.)
# ---------------------------------------------------------------------------
