"""The decision prompt states a TARGET size band, not just a cap — 2026-08-07.

Operator directive, restated with force after it kept recurring: "from
the very beginning of the project my goal has been to trade NOT
STOCKPILE CASH… why do you insist on stockpiling cash in all the
accounts when I keep saying to not build it to do this?"

The measurement behind the change (7 days to 2026-08-07, profiles
210-219):

  * The fleet held 42.8% cash ($1.30M of $3.03M) while the RANDOM
    control profiles (207-209) ran at 4.8-5.4%. A random picker was
    deploying capital and the AI was not.
  * `_size_pct_ai` — the AI's OWN raw proposal — had a median of 4.0%
    against per-profile caps of 8-13%. Nothing downstream was clipping
    it; executed open positions had a 4.11% median. The AI was choosing
    to size at half its mandate.
  * The fractional-Kelly figure fed into the prompt had a median of
    0.03 (3%), computed from an edge history P3.1 had already ruled
    untrustworthy (recorded while alt-data joins were broken). We were
    anchoring the AI low with a number documented as meaningless.

Two changes, pinned here:

  1. The Kelly block is GONE from the decision prompt. `kelly_sizing`
     still computes for the /performance page and for the
     `_size_kelly_frac` telemetry stamp — it just no longer steers.
  2. The risk-limits block states a per-conviction target band in the
     profile's own numbers, and says explicitly that sizing at the
     bottom by default is not the cautious choice.

This is the third recurrence of the cash-stockpile failure mode; the
confidence-bar docstring records an earlier one ("books at 70-92%
cash… the exact inversion of the operator's inform-don't-suppress
design"). A cap without a target reads as "smaller is safer".
"""
from __future__ import annotations

import os
import sqlite3
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _db(tmp_path, avg_value=5000, n=10):
    """Canonical journal schema — `_build_batch_prompt` reaches into
    options_roll_manager and friends, which need the real columns."""
    from journal import init_db
    db = str(tmp_path / "p.db")
    init_db(db)
    conn = sqlite3.connect(db)
    for i in range(n):
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, "
            "                    signal_type, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"2026-08-0{i % 9 + 1}T10:00", f"TICK{i}", "buy",
             100, round(avg_value / 100, 2), "BUY", "closed"),
        )
    conn.commit()
    conn.close()
    return db


def _ctx(db_path, max_position_pct=0.10):
    return SimpleNamespace(
        db_path=db_path,
        max_position_pct=max_position_pct,
        max_total_positions=10,
        enable_options=False,
        enable_short_selling=False,
        ai_confidence_threshold=45,
        segment="stocks",
        target_short_pct=0.0,
        short_max_position_pct=0.05,
    )


def _prompt(tmp_path, max_position_pct=0.10, equity=250_000):
    from ai_analyst import _build_batch_prompt
    return _build_batch_prompt(
        [{"symbol": "AAPL", "score": 70, "price": 100.0,
          "signal_type": "BUY"}],
        {"equity": equity, "cash": equity * 0.5, "num_positions": 3,
         "peak_equity": equity, "positions": []},
        {"regime": "neutral", "vix": 18.0, "spy_trend": "flat"},
        ctx=_ctx(_db(tmp_path), max_position_pct),
    )


class TestKellyNoLongerSteersTheDecision:
    def test_kelly_block_is_absent_from_the_decision_prompt(self, tmp_path):
        """It anchored a 4% median against an 8-13% mandate using a
        figure computed from data P3.1 ruled untrustworthy."""
        assert "KELLY SIZING" not in _prompt(tmp_path)

    def test_kelly_module_still_computes_for_reporting(self):
        """Removing the ANCHOR must not remove the MEASUREMENT — the
        /performance page and the `_size_kelly_frac` telemetry stamp
        both still need it."""
        from kelly_sizing import compute_kelly_recommendation  # noqa: F401
        from kelly_sizing import render_for_prompt  # noqa: F401

    def test_pipeline_still_stamps_the_kelly_reference(self):
        src = open(os.path.join(os.path.dirname(__file__), os.pardir,
                                "trade_pipeline.py")).read()
        assert "_size_kelly_frac" in src, (
            "the AI-vs-Kelly comparison must stay available for the "
            "post-fix sizing analysis"
        )


class TestPromptStatesATargetBand:
    def test_band_is_present_with_all_three_tiers(self, tmp_path):
        p = _prompt(tmp_path)
        assert "TARGET SIZE by conviction" in p
        for tier in ("high (confidence 75+)", "normal (60-74)",
                     "marginal"):
            assert tier in p, f"missing conviction tier: {tier}"

    def test_band_scales_with_the_profile_cap(self, tmp_path):
        """The band is expressed in the profile's OWN numbers, so a
        13%-cap profile is told 9.8-13.0% and an 8%-cap profile
        6.0-8.0% — not one hardcoded range for the whole fleet."""
        p10 = _prompt(tmp_path, max_position_pct=0.10)
        assert "7.5-10.0%" in p10
        p08 = _prompt(tmp_path, max_position_pct=0.08)
        assert "6.0-8.0%" in p08
        p13 = _prompt(tmp_path, max_position_pct=0.13)
        assert "9.8-13.0%" in p13

    def test_hard_cap_is_still_stated(self, tmp_path):
        """A target band must not read as permission to exceed the
        operator's cap."""
        p = _prompt(tmp_path)
        assert "Max position size" in p
        assert "primary cap" in p
        assert "never exceed it" in p

    def test_bottom_of_band_is_not_framed_as_the_safe_default(self, tmp_path):
        """The whole failure mode: 'max X or less' reads as 'smaller is
        safer'. The prompt must say otherwise in as many words."""
        p = _prompt(tmp_path)
        assert "NOT the cautious choice" in p
        assert "idle cash" in p

    def test_target_band_sits_above_the_observed_median(self, tmp_path):
        """Regression guard on the actual defect: the AI's measured
        median proposal was 4.0% on a 10% cap. The NORMAL-conviction
        floor must sit above that, or the band changes nothing."""
        p = _prompt(tmp_path, max_position_pct=0.10)
        assert "normal (60-74): 5.0-7.5%" in p
