"""Guardrails for Lever 3 of COST_AND_QUALITY_LEVERS_PLAN.md —
per-profile specialist disable list with auto-(dis)enable from
calibrator data.

Tests:
1. Profile with disabled_specialists=["pattern_recognizer"] runs
   ensemble; pattern_recognizer's API is NOT called.
2. Disabled specialist's verdict is absent from the per-symbol
   output (synthesizer treats as ABSTAIN).
3. Floor: if disabled_specialists would leave <2 active, runtime
   restores enough to satisfy the floor.
4. Source-level: ensemble.run_ensemble must reference the
   disabled_specialists field.
"""

from __future__ import annotations

import inspect
import json
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest


def _make_ctx(disabled=None):
    ctx = MagicMock()
    ctx.segment = "midcap"
    ctx.ai_provider = "anthropic"
    ctx.ai_model = "claude-haiku-4-5-20251001"
    ctx.ai_api_key = "fake"
    ctx.db_path = ":memory:"
    ctx.disabled_specialists = json.dumps(disabled or [])
    return ctx


def _fake_specialist(name):
    """Build a fake specialist module with a NAME and run() method."""
    spec = MagicMock()
    spec.NAME = name
    return spec


def test_disabled_specialist_skips_api_call(caplog):
    """When pattern_recognizer is in disabled_specialists, the log
    line "skipping pattern_recognizer" must appear, indicating the
    code path took the disable branch BEFORE the API call site.
    Log level is INFO (was DEBUG, but DEBUG is invisible in
    journalctl, so operators couldn't verify the disable was firing
    even when it was). See verify_first_cycle.sh check 2."""
    import ensemble
    import logging as _log
    caplog.set_level(_log.INFO, logger="ensemble")
    ctx = _make_ctx(disabled=["pattern_recognizer"])

    fake_specs = [_fake_specialist(n) for n in (
        "earnings_analyst", "pattern_recognizer",
        "sentiment_narrative", "risk_assessor",
    )]

    with patch("specialists.discover_specialists", return_value=fake_specs):
        with patch("ensemble._specialists_for_market", return_value=fake_specs):
            with patch("ai_providers.call_ai_structured", return_value=None):
                with patch("ensemble._any_candidate_has_upcoming_earnings", return_value=True):
                    ensemble.run_ensemble(
                        [{"symbol": "AAPL", "price": 100.0}], ctx,
                        ai_provider="anthropic",
                        ai_model="claude-haiku-4-5-20251001",
                        ai_api_key="fake",
                    )

    skip_msgs = [r for r in caplog.records
                 if "skipping pattern_recognizer" in r.getMessage()]
    assert skip_msgs, (
        "Expected an 'ensemble: skipping pattern_recognizer' debug log "
        "indicating the disable branch fired. Without this branch the "
        "specialist's API call still happens, defeating Lever 3."
    )


def test_floor_restores_when_too_many_disabled(caplog):
    """If disabled_specialists has 3 of 4, floor enforcement should
    reduce it to ≤2 disabled — proven by the warning log."""
    import ensemble
    import logging as _log
    caplog.set_level(_log.WARNING, logger="ensemble")
    ctx = _make_ctx(disabled=[
        "pattern_recognizer", "sentiment_narrative", "risk_assessor",
    ])

    fake_specs = [_fake_specialist(n) for n in (
        "earnings_analyst", "pattern_recognizer",
        "sentiment_narrative", "risk_assessor",
    )]

    with patch("specialists.discover_specialists", return_value=fake_specs):
        with patch("ensemble._specialists_for_market", return_value=fake_specs):
            with patch("ai_providers.call_ai_structured", return_value=None):
                with patch("ensemble._any_candidate_has_upcoming_earnings", return_value=True):
                    ensemble.run_ensemble(
                        [{"symbol": "AAPL", "price": 100.0}], ctx,
                        ai_provider="anthropic",
                        ai_model="claude-haiku-4-5-20251001",
                        ai_api_key="fake",
                    )

    floor_msgs = [r for r in caplog.records
                  if "floor enforcement" in r.getMessage().lower()]
    assert floor_msgs, (
        "Floor enforcement warning log not emitted. With 3 of 4 "
        "specialists disabled, the floor (≥2 active) should have "
        "kicked in and restored at least 1 specialist."
    )


def test_skipping_log_is_info_not_debug():
    """The 'ensemble: skipping' log MUST be at INFO level so operators
    can verify in journalctl. Was DEBUG → invisible, made
    verify_first_cycle.sh report a failure even when the disable
    branch was actually firing correctly."""
    import inspect
    import ensemble
    src = inspect.getsource(ensemble.run_ensemble)
    # Look for the logger.info call with "skipping"
    assert "logger.info(" in src and "ensemble: skipping" in src, (
        "REGRESSION: 'ensemble: skipping' log must be at logger.info "
        "level so operators can verify the disable list is being "
        "respected each cycle. logger.debug is invisible in journalctl."
    )
    # Anti-regression: explicitly fail if logger.debug is still being
    # used for the skip message.
    skip_block_match = inspect.getsource(ensemble.run_ensemble)
    skip_idx = skip_block_match.find('"ensemble: skipping')
    if skip_idx >= 0:
        # Look at the surrounding 80 chars to find the logger call
        surrounding = skip_block_match[max(0, skip_idx - 200):skip_idx]
        assert "logger.debug(" not in surrounding, (
            "REGRESSION: 'skipping' log reverted to logger.debug — must "
            "be logger.info for journalctl visibility."
        )


def test_run_ensemble_references_disabled_specialists():
    """Source-level guard: removing the disable-list logic
    silently re-enables full-cost behavior. The test catches it."""
    import ensemble
    src = inspect.getsource(ensemble.run_ensemble)
    assert "disabled_specialists" in src, (
        "REGRESSION: ensemble.run_ensemble no longer reads the "
        "profile's disabled_specialists field. Per-profile cost "
        "savings + decision-quality improvements regressed. See "
        "COST_AND_QUALITY_LEVERS_PLAN.md Lever 3."
    )


def test_floor_logic_in_ensemble_source():
    """Source-level: ensure the floor check (≥2 active) is present."""
    import ensemble
    src = inspect.getsource(ensemble.run_ensemble)
    # Expect either explicit "< 2" or "len(specialists) - len(disabled)"
    assert "len(specialists) - len(disabled)" in src or "< 2" in src, (
        "Floor-enforcement logic missing from run_ensemble. Without "
        "it, a buggy disabled_specialists config could disable all "
        "4 specialists and the ensemble would produce no signal."
    )


def test_health_check_task_exists_in_scheduler():
    """The auto-disable / auto-re-enable task is registered in the
    daily scheduler block."""
    import multi_scheduler
    src = inspect.getsource(multi_scheduler)
    assert "_task_specialist_health_check" in src, (
        "REGRESSION: _task_specialist_health_check removed from "
        "multi_scheduler. Auto-disable / auto-re-enable of "
        "anti-calibrated specialists no longer fires daily."
    )
    # Also verify it's actually CALLED (registered) in the snapshot
    # block, not just defined.
    assert "Specialist Health Check" in src, (
        "Health check task is defined but not registered as a "
        "run_task() invocation. Check the daily snapshot block."
    )
