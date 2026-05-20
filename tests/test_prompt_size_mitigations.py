"""Pin the 2026-05-07 cost-mitigation changes to ai_analyst.py.

Cost audit found batch_select prompt grew 49% from May 1 → May 7
($1.54/day → $2.22/day). The May 1-3 commits added:
  - Barra portfolio risk readout (always 3 stress scenarios)
  - Per-candidate Google Trends / Wiki / App Store
  - PDUFA / AdComm dates (always rendered if available)
  - macro events
  - per-candidate slippage annotations

Mitigations under test here:
1. Stress scenarios default to worst-1 (not worst-3) — cuts ~150
   tokens/cycle. Per-profile prompt_layout can override to 'brief'
   (no scenarios) or 'detailed' (worst-3).
2. PDUFA / AdComm only render when imminent (≤60d). A PDUFA 6
   months out doesn't move the AI's decision today and just bloats
   the prompt for every biotech candidate.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


class _StubCtx:
    enable_short_selling = False
    max_position_pct = 0.10
    max_total_positions = 10
    segment = "stocks"
    market_type = "stocks"
    db_path = None
    signal_weights = "{}"
    prompt_layout = "{}"
    short_max_position_pct = 0.05
    target_short_pct = 0.0
    target_book_beta = None


def _build_prompt(market_context, candidates_data=None, ctx=None):
    """Helper to build the batch prompt with default-ish inputs."""
    from ai_analyst import _build_batch_prompt
    if candidates_data is None:
        candidates_data = []
    portfolio_state = {
        "equity": 100000, "cash": 50000, "positions": [],
        "drawdown_pct": 0, "drawdown_action": "normal",
    }
    if ctx is None:
        ctx = _StubCtx()
    return _build_batch_prompt(
        candidates_data, portfolio_state, market_context, ctx=ctx,
    )


class TestStressScenarioVerbosity:
    def test_default_renders_worst_1_only(self):
        """Default verbosity = worst-1 stress scenario, not worst-3.
        Cuts ~150 tokens/cycle (caught 2026-05-07 cost audit)."""
        market_context = {
            "regime": "bull", "vix": 15, "spy_trend": "up",
            "portfolio_risk_summary": "σ 1.5%, VaR $1500",
            "portfolio_risk_scenarios": [
                "1987_blackmonday: -22.5% (worst day -20%)",
                "2008_lehman: -18% (worst day -9%)",
                "2020_covid: -12% (worst day -8%)",
            ],
        }
        prompt = _build_prompt(market_context)
        assert "1987_blackmonday" in prompt, "worst-1 should be present"
        assert "2008_lehman" not in prompt, (
            "worst-2 should NOT render at default verbosity — "
            "this is the cost-mitigation default"
        )
        assert "2020_covid" not in prompt
        assert "Worst stress scenario:" in prompt

    def test_brief_skips_scenarios_entirely(self):
        """prompt_layout 'brief' for portfolio_risk_scenarios — no
        scenarios at all (saves ~50 more tokens)."""
        market_context = {
            "regime": "bull", "vix": 15, "spy_trend": "up",
            "portfolio_risk_summary": "σ 1.5%, VaR $1500",
            "portfolio_risk_scenarios": [
                "1987_blackmonday: -22.5%",
            ],
        }
        ctx = _StubCtx()
        ctx.prompt_layout = '{"portfolio_risk_scenarios": "brief"}'
        prompt = _build_prompt(market_context, ctx=ctx)
        assert "1987_blackmonday" not in prompt
        assert "Stress scenario" not in prompt and "Worst stress" not in prompt


class TestBiotechMilestoneImminentOnly:
    """PDUFA / AdComm only render when ≤60 days out. Beyond that
    the catalyst doesn't influence today's trade decision."""

    def _candidate(self, days_to_pdufa=None, days_to_adcomm=None,
                   active_phase3_count=0):
        return {
            "symbol": "BIOX",
            "price": 10.0, "signal": "BUY", "score": 80,
            "rsi": 50, "volume_ratio": 1.5, "reason": "test",
            "votes": {"momentum": "BUY"},
            "alt_data": {
                "biotech_milestones": {
                    "days_to_pdufa": days_to_pdufa,
                    "days_to_adcomm": days_to_adcomm,
                    "drug_name": "TestDrug",
                    "active_phase3_count": active_phase3_count,
                    "adcomm_committee": "ODAC",
                },
            },
        }

    def test_pdufa_within_60d_renders(self):
        market_context = {"regime": "bull", "vix": 15, "spy_trend": "up"}
        prompt = _build_prompt(
            market_context,
            candidates_data=[self._candidate(days_to_pdufa=30)],
        )
        assert "PDUFA in 30d" in prompt

    def test_pdufa_far_out_does_not_render(self):
        """A PDUFA 180 days out shouldn't bloat every candidate
        rendering. Cost audit 2026-05-07."""
        market_context = {"regime": "bull", "vix": 15, "spy_trend": "up"}
        prompt = _build_prompt(
            market_context,
            candidates_data=[self._candidate(days_to_pdufa=180)],
        )
        assert "PDUFA" not in prompt, (
            "PDUFA 180d out should NOT render — only imminent (≤60d) "
            "catalysts add decision value"
        )

    def test_adcomm_within_60d_renders(self):
        market_context = {"regime": "bull", "vix": 15, "spy_trend": "up"}
        prompt = _build_prompt(
            market_context,
            candidates_data=[self._candidate(days_to_adcomm=21)],
        )
        assert "ODAC" in prompt and "21d" in prompt

    def test_adcomm_far_out_does_not_render(self):
        market_context = {"regime": "bull", "vix": 15, "spy_trend": "up"}
        prompt = _build_prompt(
            market_context,
            candidates_data=[self._candidate(days_to_adcomm=200)],
        )
        assert "AdComm" not in prompt and "ODAC" not in prompt
