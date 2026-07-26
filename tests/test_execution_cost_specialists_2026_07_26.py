"""Execution-cost + alt-data-silence specialists reconnect (P1.4, 2026-07-26).

Three deterministic specialists were structurally dead:

- wide_spread_caution / slippage_high_caution regexed a '%' pattern
  out of `slippage_str`, but the renderer emits
  "exec cost ~8.4 bps ($42 on this order)" — no percent sign, so
  neither fired since the format change. They now read the
  structured `slippage_estimate` dict (total_bps) the pipeline
  attaches, with a bps-string parse as fallback.

- multi_alt_data_silent counted any alt-data dict with >1 key as a
  "signal carrier", but every reader returns a multi-key dict even
  when empty of signal ({"is_cluster": false, ...},
  {"ats_volume": 0, ...}) — so signal_count was always >= 1 and the
  specialist could never fire. It now uses per-source predicates on
  the real payload shapes.

Pinned here: producer/consumer coupling through the REAL renderer
output, band boundaries, the numeric-first path, and per-source
silence semantics.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from slippage_model import render_slippage_for_prompt  # noqa: E402
from deterministic_specialists import (  # noqa: E402
    slippage_high_caution, wide_spread_caution, multi_alt_data_silent,
)


def _cand_from_renderer(total_bps, with_estimate=True):
    """Build a candidate the way trade_pipeline does: structured
    estimate + the rendered prompt string."""
    est = {"total_bps": total_bps, "slippage_dollars": 42.0}
    cand = {"signal": "BUY", "slippage_str": render_slippage_for_prompt(est)}
    if with_estimate:
        cand["slippage_estimate"] = est
    return cand


class TestSlippageSpecialists:
    def test_high_fires_on_real_renderer_output(self):
        out = slippage_high_caution.evaluate(_cand_from_renderer(45.0))
        assert out and out["severity"] == "CAUTION"

    def test_wide_fires_in_band(self):
        out = wide_spread_caution.evaluate(_cand_from_renderer(20.0))
        assert out and out["severity"] == "CAUTION"

    def test_cheap_execution_fires_neither(self):
        cand = _cand_from_renderer(8.0)
        assert slippage_high_caution.evaluate(cand) is None
        assert wide_spread_caution.evaluate(cand) is None

    def test_band_boundary_escalates_not_overlaps(self):
        """Exactly 30 bps belongs to slippage_high, not wide_spread."""
        cand = _cand_from_renderer(30.0)
        assert slippage_high_caution.evaluate(cand) is not None
        assert wide_spread_caution.evaluate(cand) is None

    def test_string_fallback_parses_bps_format(self):
        """A candidate carrying only the prompt string still works —
        and it's the REAL 'bps' format, not the dead '%' one."""
        cand = {"signal": "BUY",
                "slippage_str": "exec cost ~45.0 bps ($120 on this order)"}
        out = slippage_high_caution.evaluate(cand)
        assert out and out["severity"] == "CAUTION"

    def test_numeric_estimate_preferred_without_string(self):
        cand = {"signal": "BUY", "slippage_estimate": {"total_bps": 45.0}}
        assert slippage_high_caution.evaluate(cand) is not None

    def test_no_data_no_fire(self):
        assert slippage_high_caution.evaluate({"signal": "BUY"}) is None
        assert wide_spread_caution.evaluate({"signal": "BUY"}) is None


def _empty_bundle():
    """Real empty-of-signal payloads (multi-key, all silent)."""
    return {
        "insider": {"recent_buys": 0, "recent_sells": 0,
                     "net_direction": "neutral", "notable": None},
        "insider_cluster": {"is_cluster": False, "insider_count": 0,
                             "total_value": 0,
                             "cluster_direction": "neutral"},
        "dark_pool": {"ats_volume": 0, "ats_trade_count": 0,
                       "num_venues": 0, "week_start": None,
                       "ats_pct_of_total": None},
        "stocktwits_sentiment": {"covered": False, "message_count_7d": 0,
                                  "net_sentiment_7d": None},
        "activist_13dg": {"events": [], "count": 0, "has_13d": False},
        "recent_8k_events": {"events": [], "count": 0,
                              "high_signal_count": 0},
        "earnings_surprise": {"beat_count": 0, "miss_count": 0,
                               "total_quarters": 0},
        "congressional_recent": {"trades_60d": 0, "buys_60d": 0,
                                  "sells_60d": 0,
                                  "net_direction": "neutral"},
    }


class TestMultiAltDataSilent:
    def test_fires_on_truly_silent_multikey_payloads(self):
        """Every source present, every source empty of signal — the
        old len(v)>1 heuristic counted ALL of these as carriers and
        never fired."""
        out = multi_alt_data_silent.evaluate(
            {"signal": "BUY", "alt_data": _empty_bundle()})
        assert out and out["severity"] == "CAUTION"

    def test_one_real_carrier_suppresses(self):
        alt = _empty_bundle()
        alt["dark_pool"] = {"ats_volume": 45_704_282, "num_venues": 30,
                             "week_start": "2026-06-29",
                             "ats_pct_of_total": 17.8}
        assert multi_alt_data_silent.evaluate(
            {"signal": "BUY", "alt_data": alt}) is None

    def test_signal_rich_bundle_suppresses(self):
        """AAPL-shaped payloads (the live shapes captured 2026-07-26)."""
        alt = {
            "insider": {"recent_buys": 0, "recent_sells": 5,
                         "net_direction": "selling"},
            "options": {"has_options": True, "total_call_volume": 186449,
                         "total_put_volume": 143252, "unusual": True},
            "short": {"short_pct_float": 1.0, "short_ratio": 2.28},
            "analyst_estimates": {"eps_current_estimate": 1.89,
                                   "eps_revision_direction": "flat"},
            "stocktwits_sentiment": {"covered": True,
                                      "message_count_7d": 226,
                                      "net_sentiment_7d": 0.087},
        }
        assert multi_alt_data_silent.evaluate(
            {"signal": "BUY", "alt_data": alt}) is None

    def test_ablation_profile_no_fire(self):
        assert multi_alt_data_silent.evaluate(
            {"signal": "BUY", "alt_data": {}}) is None
        assert multi_alt_data_silent.evaluate({"signal": "BUY"}) is None

    def test_covered_flag_alone_is_not_signal(self):
        """StockTwits coverage with zero messages is a coverage fact,
        not retail attention — must not count as a carrier."""
        alt = _empty_bundle()
        alt["stocktwits_sentiment"] = {"covered": True,
                                        "message_count_7d": 0,
                                        "net_sentiment_7d": None}
        out = multi_alt_data_silent.evaluate(
            {"signal": "BUY", "alt_data": alt})
        assert out and out["severity"] == "CAUTION"

    def test_dead_sources_not_consulted(self):
        """patent_activity is disabled upstream and transcript
        sentiment only counts when it has data."""
        assert "patent_activity" not in multi_alt_data_silent._SIGNAL_TESTS
        alt = _empty_bundle()
        alt["transcript_sentiment"] = {"tone": "neutral",
                                        "key_phrases": [],
                                        "has_data": False}
        out = multi_alt_data_silent.evaluate(
            {"signal": "BUY", "alt_data": alt})
        assert out and out["severity"] == "CAUTION"
