"""2026-06-05 — three-layer institutional-style intraday risk model.

Pre-2026-06-05 behavior: `check_sector_concentration_swing` picked
the single biggest absolute sector move and, if it exceeded 3%,
issued a portfolio-wide `block_new_entries` halt. A tech -5% day
halted healthcare longs (sign-blind, sector-blind, blunt). On the
2026-06-05 reset day this blocked every AI trade despite the
healthcare ETF being +4.4% — a clear long opportunity.

These tests pin the institutional design that replaced it:

  Layer 1 (sector-specific, asymmetric) — sector ≤ -3% OR sector ≥ +6%
          halts NEW LONGS in that sector only. Healthcare +4% no
          longer halts. Tech -5% halts tech, not the whole book.
  Layer 2 (correlated spillover) — sector ≤ -5% extends its halt to
          historically correlated sectors. Tech -5% halts tech AND
          comm_services AND consumer_disc.
  Layer 3 (breadth/portfolio) — ONLY a macro event escalates to
          portfolio-wide: ≥3 sectors halted, SPY ≤ -2%, or VIX ≥ 35.
          A single hard-down sector alone does not.
"""
from __future__ import annotations

import pytest

from intraday_risk_monitor import (
    HaltDecision,
    SECTOR_CORRELATIONS,
    SECTOR_DOWN_HALT_PCT,
    SECTOR_UP_HALT_PCT,
    SECTOR_HARD_HALT_PCT,
    BREADTH_HALT_COUNT,
    SPY_BROAD_HALT_PCT,
    VIX_SPIKE_LEVEL,
    apply_correlated_spillover,
    check_breadth_collapse,
    check_sector_halts,
    compute_halt_decision,
)


# ---------------------------------------------------------------------------
# Layer 1 — asymmetric per-sector halts
# ---------------------------------------------------------------------------

class TestLayer1AsymmetricSectorHalts:

    def test_sector_down_at_threshold_halts(self):
        """-3.0% exactly should halt (boundary is inclusive on the
        downside)."""
        halted = check_sector_halts({"tech": -0.030})
        assert "tech" in halted, (
            f"tech -3.0% must halt (downside boundary). got={halted}"
        )

    def test_sector_down_below_threshold_does_not_halt(self):
        """A -2.9% sector is normal noise, not a halt."""
        halted = check_sector_halts({"tech": -0.029})
        assert "tech" not in halted, (
            f"tech -2.9% must NOT halt (below threshold). got={halted}"
        )

    def test_positive_sector_below_up_threshold_does_not_halt(self):
        """The healthcare +4.4% case that motivated the rewrite.
        Pre-fix this halted the whole portfolio. Now it must not."""
        halted = check_sector_halts({"healthcare": +0.044})
        assert "healthcare" not in halted, (
            "healthcare +4.4% must NOT halt — it's an opportunity, "
            f"not a risk. got={halted}"
        )

    def test_positive_sector_at_squeeze_threshold_halts(self):
        """Parabolic moves (+6%+) ARE a halt — squeeze / panic-buy."""
        halted = check_sector_halts({"energy": +0.061})
        assert "energy" in halted, (
            "energy +6.1% must halt (parabolic / squeeze risk). "
            f"got={halted}"
        )

    def test_threshold_asymmetry_downside_tighter_than_upside(self):
        """The whole point of the rewrite: downside threshold MUST be
        tighter than upside. Pin the asymmetry against future
        well-intentioned refactors that 'symmetrize' the rule."""
        assert SECTOR_DOWN_HALT_PCT < SECTOR_UP_HALT_PCT, (
            f"downside ({SECTOR_DOWN_HALT_PCT}%) must be tighter than "
            f"upside ({SECTOR_UP_HALT_PCT}%). The asymmetry IS the rule."
        )

    def test_returns_per_sector_dict_not_single_alert(self):
        """The old API returned ONE alert. The new API returns a
        per-sector dict so trade_pipeline can do per-trade lookups."""
        halted = check_sector_halts({
            "tech": -0.04, "finance": -0.035, "healthcare": +0.02,
        })
        assert isinstance(halted, dict)
        assert set(halted.keys()) == {"tech", "finance"}, (
            "healthcare must be excluded (+2% < 6% threshold); "
            f"got={set(halted.keys())}"
        )


# ---------------------------------------------------------------------------
# Layer 2 — correlated-sector spillover
# ---------------------------------------------------------------------------

class TestLayer2CorrelatedSpillover:

    def test_tech_hard_down_spills_to_correlated_sectors(self):
        """Tech -5% must halt tech AND comm_services AND consumer_disc.
        That's the institutional contagion model — when tech sells off
        hard, the names whose factor loadings dominate via tech (ad
        spend, discretionary spend) follow."""
        halted = check_sector_halts({"tech": -0.053})
        extended = apply_correlated_spillover(
            halted, {"tech": -0.053},
        )
        assert "comm_services" in extended, (
            f"tech -5.3% must spill to comm_services. got={extended}"
        )
        assert "consumer_disc" in extended, (
            f"tech -5.3% must spill to consumer_disc. got={extended}"
        )

    def test_soft_down_does_not_spill(self):
        """-3% trips Layer 1 but NOT spillover (needs ≤ -5%)."""
        sector_moves = {"tech": -0.035}
        halted = check_sector_halts(sector_moves)
        extended = apply_correlated_spillover(halted, sector_moves)
        assert "comm_services" not in extended, (
            "tech -3.5% must NOT spill (above hard-halt threshold). "
            f"got={extended}"
        )
        assert "consumer_disc" not in extended, (
            f"tech -3.5% must NOT spill to consumer_disc. got={extended}"
        )

    def test_spillover_reason_names_source_sector(self):
        """When a sector is halted via spillover, its reason must
        identify the SOURCE sector so the operator can trace why."""
        extended = apply_correlated_spillover(
            check_sector_halts({"tech": -0.053}),
            {"tech": -0.053},
        )
        comm_reason = extended["comm_services"]
        assert "tech" in comm_reason, (
            f"comm_services spillover reason must name 'tech'; "
            f"got: {comm_reason!r}"
        )

    def test_uncorrelated_sectors_not_spilled(self):
        """Tech -5% must NOT halt healthcare (unrelated)."""
        extended = apply_correlated_spillover(
            check_sector_halts({"tech": -0.06}),
            {"tech": -0.06},
        )
        assert "healthcare" not in extended, (
            f"healthcare must stay tradeable when tech crashes. "
            f"got={extended}"
        )


# ---------------------------------------------------------------------------
# Layer 3 — breadth-level portfolio halt
# ---------------------------------------------------------------------------

class TestLayer3BreadthCollapse:

    def test_single_sector_halt_does_NOT_trigger_portfolio_halt(self):
        """The core regression: tech -5% alone should NOT halt the
        portfolio. Without this, healthcare longs stay blocked on
        tech-led down days — which is the exact bug we fixed."""
        decision = compute_halt_decision(
            sector_moves={"tech": -0.052},
            spy_move_pct=-0.5,  # mild
            vix_level=18.0,     # normal
        )
        # Tech is halted (+ spillover)…
        assert "tech" in decision.halted_sectors
        # …but the portfolio is NOT halted
        assert decision.portfolio_action == "pass", (
            "Single-sector hard-down must NOT escalate to portfolio "
            f"halt. portfolio_action={decision.portfolio_action!r}, "
            f"halted_sectors={decision.halted_sectors}"
        )

    def test_three_sectors_halted_escalates_to_portfolio(self):
        """Breadth proves a macro event; escalate."""
        decision = compute_halt_decision(
            sector_moves={
                "tech": -0.035,
                "finance": -0.035,
                "energy": -0.035,
            },
            spy_move_pct=-0.5,
            vix_level=18.0,
        )
        assert decision.portfolio_action == "block_new_entries", (
            "3 sectors halted → portfolio-wide block_new_entries. "
            f"got={decision.portfolio_action!r}"
        )

    def test_spy_broad_drop_escalates_to_portfolio(self):
        """Even with only 1 sector halted, SPY -2% means macro event."""
        decision = compute_halt_decision(
            sector_moves={"tech": -0.035},
            spy_move_pct=-2.1,
            vix_level=20.0,
        )
        assert decision.portfolio_action == "block_new_entries"

    def test_vix_spike_escalates_to_portfolio(self):
        """VIX > 35 is a tail-risk indicator regardless of sector moves."""
        decision = compute_halt_decision(
            sector_moves={"tech": -0.01},
            spy_move_pct=-0.5,
            vix_level=42.0,
        )
        assert decision.portfolio_action == "block_new_entries"

    def test_calm_market_no_halt_anywhere(self):
        """Quiet day: no sector halts, no portfolio halt."""
        decision = compute_halt_decision(
            sector_moves={s: 0.002 for s in [
                "tech", "finance", "healthcare", "energy",
            ]},
            spy_move_pct=+0.3,
            vix_level=14.0,
        )
        assert decision.portfolio_action == "pass"
        assert decision.halted_sectors == {}


# ---------------------------------------------------------------------------
# HaltDecision — per-trade lookup interface
# ---------------------------------------------------------------------------

class TestHaltDecisionPerTradeLookup:

    def test_is_sector_halted_blocks_halted_sector(self):
        decision = compute_halt_decision(
            sector_moves={"tech": -0.053},
            spy_move_pct=-0.5, vix_level=18.0,
        )
        assert decision.is_sector_halted("tech") is True
        assert decision.is_sector_halted("comm_services") is True, (
            "Spillover-halted sectors must also block"
        )

    def test_is_sector_halted_allows_clean_sectors(self):
        """The exact case that motivated the rewrite: tech down,
        healthcare allowed."""
        decision = compute_halt_decision(
            sector_moves={
                "tech": -0.053,
                "healthcare": +0.044,
            },
            spy_move_pct=-0.5, vix_level=18.0,
        )
        assert decision.is_sector_halted("healthcare") is False, (
            "On a tech -5% day with healthcare +4%, healthcare longs "
            "MUST go through. This is the bug we fixed."
        )

    def test_portfolio_halt_blocks_all_sectors(self):
        """Breadth/SPY/VIX trigger → every sector blocked."""
        decision = compute_halt_decision(
            sector_moves={
                "tech": -0.04, "finance": -0.04, "energy": -0.04,
            },
            spy_move_pct=-2.5, vix_level=40.0,
        )
        assert decision.portfolio_action == "block_new_entries"
        # Portfolio-wide halt should block even sectors not in
        # halted_sectors (e.g. healthcare wasn't moved)
        assert decision.is_sector_halted("healthcare") is True
        assert decision.is_sector_halted("utilities") is True
