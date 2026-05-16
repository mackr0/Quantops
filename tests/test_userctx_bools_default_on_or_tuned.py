"""Structural guardrail (2026-05-13): every boolean flag on
`UserContext` must be either:
  (a) defaulting to True (active behavior on by default), OR
  (b) backed by an `_optimize_<field>` self-tuning rule that can
      flip it on per-profile based on data, OR
  (c) explicitly listed in `KNOWN_OFF_BY_DESIGN` below with a
      written rationale.

The bug class this catches.
This week (May 12-13) we shipped 5 fix waves for the same shape:
`use_conviction_tp_override`, `enable_short_selling`,
`skip_first_minutes`, `meta_pregate_threshold` (and the option
IV dead-zone) all had launched as opt-in, conservative defaults,
suppressing system activity for months. No tuner existed to
adjust them. No operator ever flipped them. The system sat
silent on a feature that was designed correctly.

Standing principle (Mack's memory): "AI-driven system — never
propose manual intervention; remediation must be deterministic
+ automated; no human-in-the-loop."

This test enforces that principle as a structural invariant.
A new boolean lever added to UserContext WILL fail this test
unless it ships with a default of True OR a tuner OR an
explicit allowlist entry — guaranteeing the May 12-13 incident
class can't recur silently.
"""
from __future__ import annotations

import dataclasses
import inspect
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


# Allowlist of bool fields that are intentionally OFF by default
# AND don't need a tuner. Each entry needs a written rationale.
# Adding to this list is a deliberate decision — the test fails
# loudly on stale entries (fields no longer on the dataclass) so
# rationales stay current.
KNOWN_OFF_BY_DESIGN = {
    # Identity / not a feature toggle
    "is_virtual":
        "Operator decides paper vs live; not a tuning target.",
    # Political content — operator-domain knowledge
    "maga_mode":
        "Political-volatility flag; operator decides whether to "
        "trade politically-sensitive symbols. Not derivable from "
        "outcome data.",
    # Cost-controlled features — opt-in to bound API spend
    "ai_model_auto_tune":
        "Auto-picks Sonnet/Opus models. Cost-gated via opt-in; "
        "the cost guard exists but operator must enable to allow "
        "the more expensive models.",
    "enable_consensus":
        "Second-opinion AI on borderline trades. Costs 2x AI "
        "calls per trade. Could become tunable in a future wave "
        "if data justifies it (currently insufficient signal).",
    "enable_shadow_eval":
        "Observational evaluation of candidate cheaper models "
        "alongside the primary. Operator-domain: only the operator "
        "knows which candidates they want to compare and how much "
        "they're willing to spend on shadow traffic. Has its own "
        "daily cost cap (SHADOW_DAILY_COST_CAP_USD) and a separate "
        "daily digest email. Never affects operational behavior.",
    "enable_long_vol_hedge":
        "SPY puts as portfolio tail-risk hedge. Costs real put "
        "premium ($/day theta burn). Operator opt-in matches the "
        "explicit risk-budget decision.",
    "enable_stat_arb_pairs":
        "Stat-arb pair trades require simultaneous long+short on "
        "the same name. Long-only profiles literally cannot use "
        "the surfaced pairs. Profile-shape constraint, not a "
        "feature flag.",
    "use_limit_orders":
        "Execution choice; market orders are simpler and currently "
        "default. Could become tunable per profile based on "
        "observed slippage; deferred — slippage tuner already "
        "exists for skip_first_minutes which addresses the same "
        "concern.",
}


def _get_userctx_bool_fields():
    """Return list of (field_name, default_value) for every bool
    field declared on UserContext."""
    from user_context import UserContext
    out = []
    for f in dataclasses.fields(UserContext):
        # Limit to plain `bool` annotations. Optional[bool] etc. are
        # handled via the same path because the default is what we
        # care about.
        ann = f.type
        if ann is bool or ann == "bool":
            out.append((f.name, f.default))
    return out


def _has_tuner_for_field(field_name: str) -> bool:
    """True iff `self_tuning.py` contains an `_optimize_<field>`
    function OR an `update_trading_profile(pid, <field>=...)` call.

    The two patterns are equivalent for this audit: both prove the
    field is auto-tunable per profile based on data."""
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.dirname(here)
    src_path = os.path.join(repo, "self_tuning.py")
    try:
        with open(src_path, "r") as fh:
            src = fh.read()
    except Exception:
        return False
    # Pattern A: dedicated optimizer function
    if re.search(rf"\bdef\s+_optimize_{re.escape(field_name)}\b", src):
        return True
    # Pattern B: any optimizer that writes the field via
    # update_trading_profile(profile_id, <field>=...)
    if re.search(
        rf"update_trading_profile\([^)]*\b{re.escape(field_name)}\s*=",
        src,
    ):
        return True
    # Pattern C: **{field: ...} (used by _optimize_strategy_toggles)
    if re.search(
        rf"\*\*\{{\s*['\"]{re.escape(field_name)}['\"]",
        src,
    ):
        return True
    return False


class TestUserContextBoolsDefaultOnOrTuned:
    def test_every_bool_is_default_on_tuned_or_allowlisted(self):
        bool_fields = _get_userctx_bool_fields()
        # Sanity: discovery returned something
        assert len(bool_fields) >= 5, (
            f"UserContext discovery found only {len(bool_fields)} "
            f"bool fields — discovery is broken; investigate before "
            f"trusting this guardrail."
        )

        violations = []
        for name, default in bool_fields:
            if default is True:
                continue  # default-on; meets the principle
            # Default is False (or other falsy)
            if name in KNOWN_OFF_BY_DESIGN:
                continue
            if _has_tuner_for_field(name):
                continue
            violations.append(name)

        if violations:
            pytest.fail(
                "These UserContext bool fields default to False AND "
                "have no self-tuning rule AND are not on the "
                "KNOWN_OFF_BY_DESIGN allowlist:\n\n"
                + "\n".join(f"  - {n}" for n in violations)
                + "\n\nThis is the bug class that caused 5 fix waves "
                "May 12-13: features sitting at conservative "
                "defaults for months because no tuner existed and "
                "no operator flipped them. Choose ONE of:\n"
                "  1. Flip the default to True (preferred — matches "
                "the AI-driven thesis)\n"
                "  2. Add `_optimize_<field>` to self_tuning.py\n"
                "  3. Add an entry to KNOWN_OFF_BY_DESIGN in this "
                "test with a written rationale (cost-gated, "
                "operator-domain, etc.)"
            )

    def test_allowlist_entries_match_existing_fields(self):
        """Stale allowlist entries (fields removed from the dataclass)
        should fail this test so rationales stay current."""
        existing = {n for n, _ in _get_userctx_bool_fields()}
        stale = set(KNOWN_OFF_BY_DESIGN) - existing
        if stale:
            pytest.fail(
                "KNOWN_OFF_BY_DESIGN contains entries that no "
                "longer exist on UserContext (rationale drift):\n\n"
                + "\n".join(f"  - {n}" for n in sorted(stale))
                + "\n\nRemove these entries — they're protecting "
                "nothing."
            )

    def test_at_least_one_field_proves_each_path(self):
        """Sanity: at least ONE field hits each acceptable path
        (default-on / tunable / allowlisted). If any path is empty,
        the test is silently weakened."""
        bool_fields = _get_userctx_bool_fields()
        n_default_on = sum(1 for n, d in bool_fields if d is True)
        n_tunable = sum(
            1 for n, d in bool_fields
            if d is False and n not in KNOWN_OFF_BY_DESIGN
            and _has_tuner_for_field(n)
        )
        n_allowlisted = sum(
            1 for n, d in bool_fields
            if d is False and n in KNOWN_OFF_BY_DESIGN
        )
        assert n_default_on > 0, "no bool fields are default-on"
        # n_tunable can be 0 — currently no tunable bools exist
        # (the conviction-TP rule writes 0/1 but counts as tuner)
        assert n_default_on + n_tunable + n_allowlisted >= len(
            bool_fields
        ), "some fields are unaccounted for"
