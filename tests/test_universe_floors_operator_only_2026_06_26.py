"""Universe / liquidity floors are OPERATOR-ONLY — never auto-tuned (2026-06-26).

Governance contract: the self-tuner optimizes HOW to trade (confidence,
stops, sizing, signal weights); the operator owns WHAT is eligible to
trade. A self-tuner that could relax its own liquidity floor would walk
it down to chase entry count — the classic "optimizer defeats its own
risk limit." So min_price / max_price / min_volume / min_adv are settable
only from the Settings page.

This is enforced as a CLASS, not per-param: every tuner write funnels
through `_apply_param_change`, which refuses these params — so the rule
holds for the optimizer dispatch, insight propagation, AND any future
write path. These tests pin that the firewall exists, the two retired
optimizers stay deleted, and nothing re-registers them.
"""
from __future__ import annotations

import inspect
from unittest.mock import patch

import pytest

import self_tuning

FLOORS = ("min_price", "max_price", "min_volume", "min_adv")


def test_floors_are_in_the_operator_only_set():
    for p in FLOORS:
        assert p in self_tuning._OPERATOR_ONLY_PARAMS, (
            f"{p} must be operator-only (never auto-tuned)")


@pytest.mark.parametrize("param", FLOORS)
def test_apply_param_change_refuses_floor_writes(param):
    """The single gatekeeper must refuse a floor write WITHOUT touching
    the DB or logging a tuning_history row — and report it unchanged."""
    with patch("models.update_trading_profile") as mock_update, \
         patch("models.log_tuning_change") as mock_log, \
         patch("models.record_param_reference_if_absent"):
        applied, was_clamped, suffix = self_tuning._apply_param_change(
            profile_id=1, user_id=1, adjustment_type="should_be_refused",
            param_name=param, old_value=5_000_000, proposed_new_value=1_000,
            reason="adversarial: try to lower the floor",
        )
    assert applied == 5_000_000, "refused write must return the old value unchanged"
    assert was_clamped is False
    assert "operator-only" in suffix
    mock_update.assert_not_called()
    mock_log.assert_not_called()


@pytest.mark.parametrize("name", ["_optimize_price_band", "_optimize_min_volume"])
def test_retired_optimizers_stay_deleted(name):
    assert not hasattr(self_tuning, name), (
        f"{name} was deleted 2026-06-26 — re-adding it re-opens auto-tuning "
        f"of an operator-only universe floor")


def test_floors_not_in_optimizer_direction_registry():
    direction = self_tuning._OPTIMIZER_DIRECTION
    for bad in ("_optimize_price_band", "_optimize_min_volume"):
        assert bad not in direction


def test_dispatch_list_does_not_reference_retired_optimizers():
    src = inspect.getsource(self_tuning._apply_upward_optimizations)
    # The registry references functions by bare name; ensure neither
    # retired optimizer is named as a dispatch target (comments are fine).
    for bad in ("_optimize_price_band", "_optimize_min_volume"):
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert bad not in stripped, (
                f"{bad} must not be a dispatch target in "
                f"_apply_upward_optimizations: {stripped!r}")


@pytest.mark.parametrize("change_type", [
    "price_band_min_raise", "price_band_max_lower", "min_volume_raise",
])
def test_floor_changes_are_not_propagatable_to_peers(change_type):
    from insight_propagation import _detector_for
    assert _detector_for(change_type) is None, (
        f"{change_type} maps to a floor optimizer — it must not propagate to "
        f"peer profiles (universe floors are operator-only)")


# ---------------------------------------------------------------------------
# The SECOND tuner write path: the auto-reversal in apply_auto_adjustments
# bypasses _apply_param_change, so it needs its own guard. (Found by
# adversarial review — the firewall test above does NOT cover this path.)
# An empty DB has no ai_predictions table, so apply_auto_adjustments runs
# only the reversal loop then returns early — isolating it cleanly.
# ---------------------------------------------------------------------------

from types import SimpleNamespace


def _worsened_row(param, old_value, new_value):
    return [{
        "parameter_name": param, "old_value": old_value,
        "new_value": new_value, "outcome_after": "worsened",
        "win_rate_at_change": 50, "win_rate_after": 30,
        "change_type": f"{param}_change", "adjustment_type": f"{param}_change",
    }]


def test_auto_reversal_skips_operator_only_floor(tmp_path, monkeypatch):
    """A historical 'worsened' min_volume tightening must NOT be auto-reversed
    (that would loosen the operator's floor back toward its old value)."""
    db = str(tmp_path / "p.db")
    import sqlite3
    sqlite3.connect(db).close()  # valid empty DB → early return after reversal
    writes = []
    monkeypatch.setattr("models.review_past_adjustments",
                        lambda *a, **k: _worsened_row("min_volume", "500000", "1000000"))
    monkeypatch.setattr("models.update_trading_profile",
                        lambda pid, **kw: writes.append(kw))
    monkeypatch.setattr("models.log_tuning_change", lambda *a, **k: 1)
    ctx = SimpleNamespace(profile_id=1, user_id=1, db_path=db,
                          enable_self_tuning=True)
    self_tuning.apply_auto_adjustments(ctx)
    assert all("min_volume" not in w for w in writes), (
        "auto-reversal wrote the operator-only min_volume floor: %r" % writes)


def test_auto_reversal_still_reverses_a_normal_param(tmp_path, monkeypatch):
    """Control: the reversal mechanism still works for a tunable param —
    proves the floor-skip is targeted, not a blanket disable."""
    db = str(tmp_path / "p.db")
    import sqlite3
    sqlite3.connect(db).close()
    writes = []
    monkeypatch.setattr(
        "models.review_past_adjustments",
        lambda *a, **k: _worsened_row("ai_confidence_threshold", "25", "50"))
    monkeypatch.setattr("models.update_trading_profile",
                        lambda pid, **kw: writes.append(kw))
    monkeypatch.setattr("models.log_tuning_change", lambda *a, **k: 1)
    ctx = SimpleNamespace(profile_id=1, user_id=1, db_path=db,
                          enable_self_tuning=True)
    self_tuning.apply_auto_adjustments(ctx)
    assert any("ai_confidence_threshold" in w for w in writes), (
        "the reversal mechanism must still reverse a normal tunable param")


def test_maga_oversold_scan_enforces_universe_floors():
    """The MAGA-mode oversold scan is a SECOND stock-entry path that bypasses
    screen_by_price_range — it must enforce the same price/volume/ADV floors
    (found by adversarial review). Structural pin (the gate logic itself is
    behaviourally covered by the screener ADV tests)."""
    import inspect
    import multi_scheduler
    src = inspect.getsource(multi_scheduler._get_shared_candidates)
    # The MAGA oversold scan lives in the shared candidate builder.
    assert "MAGA" in src
    for floor in ("ctx.min_adv", "ctx.min_price", "ctx.max_price", "ctx.min_volume"):
        assert floor in src, (
            f"MAGA oversold scan must gate additions on {floor} (else thin/cheap "
            f"oversold names reach live trading below the operator's floors)")
