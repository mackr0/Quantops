"""Guardrail: AI predictions must NOT resolve to win/loss until
they have aged at least MIN_HOLD_DAYS_BEFORE_RESOLVE trading days.

History: 2026-04-27 methodology audit. The original `_resolve_one`
checked the ±2% win/loss thresholds against the current price
regardless of how recently the prediction was made. A BUY made at
10am that drifted +2% by 11am resolved as "win" within an hour —
the label captured intraday noise rather than a meaningful forward-
horizon outcome. With a 2% threshold and typical retail-cap
volatility, many predictions resolved on noise.

Wave 1 / Fix #6 of METHODOLOGY_FIX_PLAN.md added a forward-horizon
gate: BUY/SELL predictions cannot resolve until they've aged
MIN_HOLD_DAYS_BEFORE_RESOLVE trading days (default 5).

These tests prove:

1. The constant exists and is at least 1.
2. A young BUY that's already crossed the +2% threshold returns
   None (still pending) — not a win.
3. A young SELL that's already crossed the threshold also stays
   pending.
4. After the horizon, the same prediction resolves correctly.
5. HOLD's existing horizon (HOLD_RESOLVE_DAYS) is preserved.
6. The TIMEOUT_DAYS escape hatch still force-resolves to neutral.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta

import ai_tracker


# ---------------------------------------------------------------------------
# Source-level guardrails
# ---------------------------------------------------------------------------

def test_min_hold_constant_exists_and_is_meaningful():
    assert hasattr(ai_tracker, "MIN_HOLD_DAYS_BEFORE_RESOLVE"), (
        "REGRESSION: ai_tracker.MIN_HOLD_DAYS_BEFORE_RESOLVE removed. "
        "Without this gate, BUY/SELL predictions resolve on intraday "
        "noise within hours — the bug Wave 1 / Fix #6 of "
        "METHODOLOGY_FIX_PLAN.md fixed."
    )
    assert ai_tracker.MIN_HOLD_DAYS_BEFORE_RESOLVE >= 1, (
        f"MIN_HOLD_DAYS_BEFORE_RESOLVE={ai_tracker.MIN_HOLD_DAYS_BEFORE_RESOLVE} "
        f"is too small to filter intraday noise. Recommended ≥ 3."
    )


def test_resolve_one_references_min_hold_constant():
    """The constant must actually be USED, not just defined."""
    src = inspect.getsource(ai_tracker._resolve_one)
    assert "MIN_HOLD_DAYS_BEFORE_RESOLVE" in src, (
        "REGRESSION: _resolve_one no longer references the forward-"
        "horizon gate. Without this check, intraday noise resolutions "
        "return."
    )


# ---------------------------------------------------------------------------
# Behavioral: young predictions stay pending even if threshold crossed
# ---------------------------------------------------------------------------

def _make_prediction(signal, hours_ago, pred_price=100.0):
    """Build a row dict shaped like the SQL row in resolve_predictions."""
    ts = (datetime.utcnow() - timedelta(hours=hours_ago)).isoformat()
    return {
        "predicted_signal": signal,
        "price_at_prediction": pred_price,
        "timestamp": ts,
    }


def test_young_buy_above_threshold_does_not_resolve_as_win():
    """A BUY made 1 hour ago, current price +2.5% above prediction:
    the OLD code returned ('win', 2.5, 0). The NEW code must return
    None — too young to resolve."""
    pred = _make_prediction("BUY", hours_ago=1, pred_price=100.0)
    result = ai_tracker._resolve_one(pred, current_price=102.5)
    assert result is None, (
        f"BUY made 1 hour ago resolved as {result} — should be None "
        f"(under MIN_HOLD_DAYS_BEFORE_RESOLVE). The gate is broken; "
        f"predictions are resolving on intraday noise again."
    )


def test_young_buy_below_threshold_does_not_resolve_as_loss():
    pred = _make_prediction("BUY", hours_ago=1, pred_price=100.0)
    result = ai_tracker._resolve_one(pred, current_price=97.5)
    assert result is None, (
        f"BUY made 1 hour ago resolved as {result} — should be None"
    )


def test_young_sell_above_threshold_does_not_resolve():
    """SELL profits when price drops. A SELL 1 hour ago with price
    -3% must stay pending."""
    pred = _make_prediction("SELL", hours_ago=1, pred_price=100.0)
    result = ai_tracker._resolve_one(pred, current_price=97.0)
    assert result is None


# ---------------------------------------------------------------------------
# Behavioral: aged predictions DO resolve
# ---------------------------------------------------------------------------

def test_aged_buy_above_threshold_resolves_as_win():
    """After MIN_HOLD_DAYS_BEFORE_RESOLVE trading days, the same
    +2.5% BUY must resolve as win. The horizon is GATING noise, not
    blocking real wins."""
    # 7 calendar days ≈ 5 trading days
    pred = _make_prediction("BUY", hours_ago=7 * 24, pred_price=100.0)
    result = ai_tracker._resolve_one(pred, current_price=103.0)
    assert result is not None, (
        "Aged BUY at +3% should resolve, not stay pending"
    )
    assert result[0] == "win", f"Expected win, got {result[0]}"


def test_aged_buy_below_threshold_resolves_as_loss():
    pred = _make_prediction("BUY", hours_ago=7 * 24, pred_price=100.0)
    result = ai_tracker._resolve_one(pred, current_price=97.0)
    assert result is not None
    assert result[0] == "loss"


# ---------------------------------------------------------------------------
# Behavioral: HOLD path is unchanged (already had its own horizon)
# ---------------------------------------------------------------------------

def test_hold_horizon_unchanged_pending():
    """HOLD's existing HOLD_RESOLVE_DAYS gate must keep working."""
    pred = _make_prediction("HOLD", hours_ago=1, pred_price=100.0)
    result = ai_tracker._resolve_one(pred, current_price=100.5)
    assert result is None


def test_hold_horizon_unchanged_resolves():
    """HOLD that's been quiet for ≥ HOLD_RESOLVE_DAYS, with price
    inside the band, resolves as a win."""
    days = ai_tracker.HOLD_RESOLVE_DAYS + 1  # well past the horizon
    pred = _make_prediction("HOLD", hours_ago=days * 24 * 7 // 5,
                            pred_price=100.0)
    result = ai_tracker._resolve_one(pred, current_price=100.5)
    assert result is not None
    assert result[0] == "win"


# ---------------------------------------------------------------------------
# Behavioral: TIMEOUT escape hatch still works
# ---------------------------------------------------------------------------

def test_timeout_force_resolves_to_neutral():
    """A pending BUY that's aged past TIMEOUT_DAYS without hitting a
    threshold force-resolves to neutral. Gate must not block this."""
    days = ai_tracker.TIMEOUT_DAYS + 5
    pred = _make_prediction("BUY", hours_ago=days * 24 * 7 // 5,
                            pred_price=100.0)
    # Price moved less than the win/loss thresholds
    result = ai_tracker._resolve_one(pred, current_price=100.5)
    assert result is not None
    assert result[0] == "neutral"
