"""ai_analyst.py:815 TODO — "execution requires the AI to propose
with action='OPTIONS' (deferred to follow-up)" — was outdated. The
follow-up shipped in two stages:

  1. ai_analyst._build_batch_prompt (2026-05-19): the prompt's
     "Actions allowed" string includes OPTIONS, the OPTIONS note
     gives full required-fields documentation, and the JSON example
     shows the action='OPTIONS' shape — but ONLY when the prompt
     actually carries enough options context for the AI to make
     informed proposals (`options_strategy_block` non-empty OR
     any candidate has `options_oracle_summary`).

  2. trade_pipeline.run_trade_cycle (2026-05-19): when the AI
     response contains `action='OPTIONS'`, the dispatch loop sends
     it to `OptionPipeline._execute_single_leg` — the same code
     path the new pipeline-dispatch architecture uses.

This file pins (1) end-to-end via the actual `_build_batch_prompt`
call; (2) is already covered by
`test_pipelines_b_complete_2026_05_19.py` and
`test_single_leg_options_migration_2026_05_19.py`.

The TODO comment at ai_analyst.py:815 is also deleted as part of
the same commit — the test below stays as the durable contract.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


def _ctx_with_options_enabled():
    return SimpleNamespace(
        enable_short_selling=False,
        enable_options=True,
        max_position_pct=0.10,
        ai_confidence_threshold=50,
        # Fields the prompt builder reads with getattr defaults — set
        # the most-referenced ones explicitly so the test doesn't
        # silently exercise a different code path.
        target_short_pct=0.0,
        short_max_position_pct=0.05,
    )


def _candidate_with_options_oracle(symbol="AAPL"):
    """Minimum candidate shape that triggers options_action_enabled
    via the per-candidate options_oracle_summary path."""
    return {
        "symbol": symbol,
        "price": 150.0,
        "signal": "STRONG_BUY",
        "score": 0.8,
        "rsi": 60, "volume_ratio": 1.2, "atr": 1.0, "adx": 25,
        "stoch_rsi": 50, "roc_10": 1.0, "pct_from_52w_high": 0.05,
        "mfi": 50, "cmf": 0, "squeeze": 0, "pct_from_vwap": 0,
        "nearest_fib_dist": 99, "gap_pct": 0,
        "options_oracle_summary": (
            f"{symbol} IV rank 35; DTE 30; GEX positive"
        ),
    }


def _candidate_no_options(symbol="ZZZZ"):
    """Same shape but NO options data — verifies the prompt
    correctly DROPS the OPTIONS action when there's nothing to
    propose against."""
    cand = _candidate_with_options_oracle(symbol)
    cand.pop("options_oracle_summary", None)
    return cand


# ---------------------------------------------------------------------------
# Layer 1 — the actions string includes OPTIONS when triggered
# ---------------------------------------------------------------------------

def test_actions_string_includes_OPTIONS_when_candidate_has_oracle():
    """The AI prompt's `Actions allowed: BUY | OPTIONS | ...` line MUST
    include OPTIONS the moment any candidate carries an oracle summary.
    Without OPTIONS in the actions list, the AI doesn't know it's
    allowed to propose option trades natively."""
    from ai_analyst import _build_batch_prompt
    cands = [_candidate_with_options_oracle()]
    with patch("options_strategy_advisor.render_for_prompt",
                return_value=""):
        # Force advisor block empty so we test the per-candidate
        # oracle path in isolation.
        prompt = _build_batch_prompt(
            cands,
            portfolio_state={"positions": [], "drawdown_pct": 0.0,
                              "account": {"equity": 100000}},
            market_context={"regime": "neutral"},
            ctx=_ctx_with_options_enabled(),
        )
    assert "Actions allowed:" in prompt, (
        "Prompt structure changed — `Actions allowed:` line missing. "
        "Test anchor broke; investigate prompt builder."
    )
    actions_line = [
        ln for ln in prompt.split("\n") if "Actions allowed:" in ln
    ][0]
    assert "OPTIONS" in actions_line, (
        f"OPTIONS missing from actions string when candidate carries "
        f"an options_oracle_summary. Without this the AI can never "
        f"propose option trades natively. Got: {actions_line!r}"
    )


def test_OPTIONS_dropped_from_actions_when_no_options_context():
    """The flip side: when no candidate has options context AND the
    advisor block is empty, OPTIONS must NOT appear. Otherwise the
    AI proposes options against names it has no oracle data for —
    pure guessing."""
    from ai_analyst import _build_batch_prompt
    with patch("options_strategy_advisor.render_for_prompt",
                return_value=""):
        prompt = _build_batch_prompt(
            [_candidate_no_options()],
            portfolio_state={"positions": [], "drawdown_pct": 0.0,
                              "account": {"equity": 100000}},
            market_context={"regime": "neutral"},
            ctx=_ctx_with_options_enabled(),
        )
    actions_line = [
        ln for ln in prompt.split("\n") if "Actions allowed:" in ln
    ][0]
    assert "OPTIONS" not in actions_line, (
        f"OPTIONS appeared in actions string with no options "
        f"context — AI has no reference for what to propose. "
        f"Got: {actions_line!r}"
    )


# ---------------------------------------------------------------------------
# Layer 2 — the prompt carries the OPTIONS example JSON
# ---------------------------------------------------------------------------

def test_prompt_includes_OPTIONS_example_json_when_action_enabled():
    """The AI needs the JSON shape (option_strategy / strike /
    expiry / contracts) to produce parseable proposals. The example
    snippet must be present whenever OPTIONS is allowed."""
    from ai_analyst import _build_batch_prompt
    with patch("options_strategy_advisor.render_for_prompt",
                return_value=""):
        prompt = _build_batch_prompt(
            [_candidate_with_options_oracle()],
            portfolio_state={"positions": [], "drawdown_pct": 0.0,
                              "account": {"equity": 100000}},
            market_context={"regime": "neutral"},
            ctx=_ctx_with_options_enabled(),
        )
    assert '"action": "OPTIONS"' in prompt, (
        "The JSON example `action: OPTIONS` snippet must be in the "
        "prompt when OPTIONS is enabled. Without it the AI knows the "
        "action exists (from the actions list) but doesn't know the "
        "required field shape — produces malformed proposals that "
        "get filtered by _validate_ai_trades."
    )
    # And the required fields enumerated in the OPTIONS note
    assert "option_strategy" in prompt
    assert "strike" in prompt
    assert "expiry" in prompt
    assert "contracts" in prompt


def test_OPTIONS_note_uses_inviting_framing_not_only_propose():
    """Per the 2026-05-19 asset-class-neutrality work, the OPTIONS
    note must use the same 'take as-is / adjust / propose' framing
    as stocks. The pre-2026-05-19 phrasing 'only propose when…'
    biased the AI away from options."""
    from ai_analyst import _build_batch_prompt
    with patch("options_strategy_advisor.render_for_prompt",
                return_value=""):
        prompt = _build_batch_prompt(
            [_candidate_with_options_oracle()],
            portfolio_state={"positions": [], "drawdown_pct": 0.0,
                              "account": {"equity": 100000}},
            market_context={"regime": "neutral"},
            ctx=_ctx_with_options_enabled(),
        )
    # Find the OPTIONS note paragraph
    lower = prompt.lower()
    # The OPTIONS note explicitly contains the parallel phrasing
    assert "take them as-is" in lower or "take as-is" in lower, (
        "OPTIONS note lost the inviting 'take as-is' framing that "
        "matches the stocks/multileg notes; asymmetric framing "
        "silently re-biases the AI away from options."
    )


# ---------------------------------------------------------------------------
# Layer 3 — structural pin: the stale TODO comment must stay gone
# ---------------------------------------------------------------------------

def test_stale_options_deferred_comment_was_removed_from_ai_analyst():
    """The comment 'execution requires the AI to propose with
    action='OPTIONS' (deferred to follow-up)' was the marker for
    work that's since shipped. Leaving it in misleads future
    readers into thinking the OPTIONS path is unimplemented.

    If this test fails, someone re-introduced the deferred comment;
    delete it again — the tests above prove the path works."""
    src = (REPO_ROOT / "ai_analyst.py").read_text()
    assert "deferred to follow-up" not in src, (
        "ai_analyst.py contains the literal phrase 'deferred to "
        "follow-up' — historically the marker for the OPTIONS "
        "action vocab gap. That gap has shipped (see this test "
        "file's other assertions). Delete the comment."
    )
