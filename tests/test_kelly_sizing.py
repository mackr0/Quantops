"""P4.2 of LONG_SHORT_PLAN.md — Kelly position sizing tests."""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


# ---------------------------------------------------------------------------
# compute_kelly_fraction
# ---------------------------------------------------------------------------

def test_kelly_classic_example():
    """Classic Kelly: 55% win rate, 2:1 reward/risk → full Kelly = 0.325."""
    from kelly_sizing import compute_kelly_fraction
    full = compute_kelly_fraction(win_rate=0.55, avg_win=0.10,
                                    avg_loss=0.05, fractional=1.0)
    # b = 2, p = 0.55, q = 0.45 → (2*0.55 - 0.45)/2 = 0.325
    assert full == pytest.approx(0.325, abs=0.001)


def test_kelly_quarter_kelly_default():
    from kelly_sizing import compute_kelly_fraction
    quarter = compute_kelly_fraction(0.55, 0.10, 0.05)  # default 0.25
    full = compute_kelly_fraction(0.55, 0.10, 0.05, fractional=1.0)
    assert quarter == pytest.approx(full * 0.25, abs=0.001)


def test_kelly_returns_none_on_no_edge():
    """50% win rate at 1:1 → zero edge → don't trade."""
    from kelly_sizing import compute_kelly_fraction
    assert compute_kelly_fraction(0.50, 0.10, 0.10) is None


def test_kelly_returns_none_on_negative_edge():
    """Win rate 40% at 1:1 → negative Kelly → don't trade."""
    from kelly_sizing import compute_kelly_fraction
    assert compute_kelly_fraction(0.40, 0.10, 0.10) is None


def test_kelly_caps_unreasonable_values_in_fractional_mode():
    """If even the safety-multiplied Kelly comes out >50% of capital,
    return None — inputs are extreme. Cap only applies in fractional
    mode (< 1.0); in report mode (=1.0) the function returns full
    Kelly so callers can compute their own multiplier."""
    from kelly_sizing import compute_kelly_fraction
    # 95% win rate, 10:1 ratio → full Kelly = 0.945
    # quarter Kelly = 0.236 (well under 50%) — but say we ask for 0.75x
    # → 0.709 > 0.50 → return None.
    assert compute_kelly_fraction(0.95, 1.0, 0.10, fractional=0.75) is None
    # In report mode (=1.0), we DO return the full Kelly so the caller
    # can compute its own multiplier without losing the data.
    full = compute_kelly_fraction(0.95, 1.0, 0.10, fractional=1.0)
    assert full == pytest.approx(0.945, abs=0.001)


def test_kelly_handles_zero_inputs():
    from kelly_sizing import compute_kelly_fraction
    assert compute_kelly_fraction(0.0, 0.10, 0.05) is None
    assert compute_kelly_fraction(0.55, 0.0, 0.05) is None
    assert compute_kelly_fraction(0.55, 0.10, 0.0) is None
    assert compute_kelly_fraction(1.0, 0.10, 0.05) is None  # 100% — invalid


# ---------------------------------------------------------------------------
# compute_kelly_recommendation
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_predictions_db(tmp_path):
    db = str(tmp_path / "p.db")
    from journal import init_db
    init_db(db)
    return db


def _seed_predictions(db_path, predictions):
    """predictions = list of (predicted_signal, prediction_type,
    actual_outcome, actual_return_pct)."""
    conn = sqlite3.connect(db_path)
    for sig, ptype, outcome, ret in predictions:
        conn.execute(
            "INSERT INTO ai_predictions "
            "(timestamp, symbol, predicted_signal, prediction_type, "
            " price_at_prediction, status, actual_outcome, actual_return_pct) "
            "VALUES ('2026-01-01', 'X', ?, ?, 100, 'resolved', ?, ?)",
            (sig, ptype, outcome, ret),
        )
    conn.commit()
    conn.close()


def test_recommendation_none_below_min_samples(tmp_predictions_db):
    from kelly_sizing import compute_kelly_recommendation
    # Only 5 predictions — below the 30 minimum
    preds = [("BUY", "directional_long", "win", 5.0)] * 5
    _seed_predictions(tmp_predictions_db, preds)
    assert compute_kelly_recommendation(tmp_predictions_db, "long") is None


def test_recommendation_returns_kelly_with_enough_data(tmp_predictions_db):
    """30 long predictions: 60% win rate, +5% avg win, -3% avg loss."""
    from kelly_sizing import compute_kelly_recommendation
    preds = (
        [("BUY", "directional_long", "win", 5.0)] * 18 +    # 60% win rate
        [("BUY", "directional_long", "loss", -3.0)] * 12
    )
    _seed_predictions(tmp_predictions_db, preds)
    rec = compute_kelly_recommendation(tmp_predictions_db, "long")
    assert rec is not None
    assert rec["win_rate"] == pytest.approx(0.6, abs=0.01)
    assert rec["avg_win_pct"] == pytest.approx(0.05, abs=0.001)
    assert rec["avg_loss_pct"] == pytest.approx(0.03, abs=0.001)
    assert rec["n"] == 30
    # full Kelly: b=5/3=1.667, p=0.6, q=0.4 → (1.667*0.6 - 0.4)/1.667 = 0.36
    assert rec["full_kelly"] == pytest.approx(0.36, abs=0.01)
    # quarter Kelly = 0.36 * 0.25 = 0.09
    assert rec["fractional_kelly"] == pytest.approx(0.09, abs=0.01)


def test_recommendation_separates_long_and_short(tmp_predictions_db):
    """Short-direction recommendation reads only directional_short rows."""
    from kelly_sizing import compute_kelly_recommendation
    preds = (
        [("BUY", "directional_long", "win", 5.0)] * 20 +
        [("BUY", "directional_long", "loss", -3.0)] * 15 +
        [("SHORT", "directional_short", "win", 4.0)] * 25 +
        [("SHORT", "directional_short", "loss", -2.0)] * 10
    )
    _seed_predictions(tmp_predictions_db, preds)
    rec_long = compute_kelly_recommendation(tmp_predictions_db, "long")
    rec_short = compute_kelly_recommendation(tmp_predictions_db, "short")
    assert rec_long["n"] == 35  # 20W + 15L
    assert rec_short["n"] == 35  # 25W + 10L
    # Different stats → different recommendations
    assert rec_long["win_rate"] != rec_short["win_rate"]


def test_recommendation_falls_back_for_legacy_rows_without_prediction_type(
    tmp_predictions_db,
):
    """Rows from before the P1.0 backfill have prediction_type=NULL —
    the inferred-from-signal logic should still classify them."""
    from kelly_sizing import compute_kelly_recommendation
    preds = (
        [("BUY", None, "win", 5.0)] * 20 +
        [("BUY", None, "loss", -3.0)] * 15
    )
    _seed_predictions(tmp_predictions_db, preds)
    rec = compute_kelly_recommendation(tmp_predictions_db, "long")
    assert rec is not None
    assert rec["n"] == 35


def test_recommendation_excludes_hold_predictions(tmp_predictions_db):
    """HOLD predictions are tagged directional_long but represent
    'keep current position' — not new entries. Their outcomes reflect
    existing position drift, not Kelly-relevant edge. Must be filtered
    out so the Kelly stats only reflect actual entries."""
    from kelly_sizing import compute_kelly_recommendation
    # 30 active BUYs with strong positive edge (70% WR, 4% win, 2% loss):
    preds = (
        [("BUY", "directional_long", "win", 4.0)] * 21 +
        [("BUY", "directional_long", "loss", -2.0)] * 9 +
        # Plus a flood of HOLD rows tagged directional_long with NEGATIVE
        # outcomes. If HOLDs were counted, the win rate would crater
        # and edge would go negative.
        [("HOLD", "directional_long", "loss", -5.0)] * 500 +
        [("HOLD", "directional_long", "win", 0.5)] * 100
    )
    _seed_predictions(tmp_predictions_db, preds)
    rec = compute_kelly_recommendation(tmp_predictions_db, "long")
    assert rec is not None, "HOLDs must not crowd out the real BUY edge"
    assert rec["n"] == 30  # only BUYs counted, not HOLDs
    assert rec["win_rate"] == pytest.approx(0.70, abs=0.01)


def test_recommendation_none_when_no_negative_edge(tmp_predictions_db):
    """If win rate × win - (1-win_rate) × loss is non-positive, no
    recommendation."""
    from kelly_sizing import compute_kelly_recommendation
    # 30% win rate, 1:1 → negative edge
    preds = (
        [("BUY", "directional_long", "win", 5.0)] * 9 +
        [("BUY", "directional_long", "loss", -5.0)] * 21
    )
    _seed_predictions(tmp_predictions_db, preds)
    rec = compute_kelly_recommendation(tmp_predictions_db, "long")
    assert rec is None


# ---------------------------------------------------------------------------
# render_for_prompt
# ---------------------------------------------------------------------------

def test_render_empty_returns_empty_string():
    from kelly_sizing import render_for_prompt
    assert render_for_prompt(None, None) == ""


def test_render_long_only_book():
    from kelly_sizing import render_for_prompt
    rec_long = {
        "win_rate": 0.65, "avg_win_pct": 0.04, "avg_loss_pct": 0.025,
        "n": 100, "full_kelly": 0.35,
        "fractional_kelly": 0.0875, "fraction_used": 0.25,
    }
    rendered = render_for_prompt(rec_long, None)
    assert "KELLY SIZING" in rendered
    assert "LONG: Kelly" in rendered
    assert "SHORT" not in rendered


def test_render_includes_both_when_present():
    from kelly_sizing import render_for_prompt
    rec_long = {"win_rate": 0.65, "avg_win_pct": 0.04, "avg_loss_pct": 0.025,
                "n": 100, "full_kelly": 0.35, "fractional_kelly": 0.0875,
                "fraction_used": 0.25}
    rec_short = {"win_rate": 0.55, "avg_win_pct": 0.05, "avg_loss_pct": 0.04,
                  "n": 80, "full_kelly": 0.20, "fractional_kelly": 0.05,
                  "fraction_used": 0.25}
    rendered = render_for_prompt(rec_long, rec_short)
    assert "LONG: Kelly 8.8%" in rendered
    assert "SHORT: Kelly 5.0%" in rendered
