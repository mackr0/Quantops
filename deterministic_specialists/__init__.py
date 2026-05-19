"""Deterministic specialists — Phase 3 of docs/17.

A library of pure-Python pattern-matching rules. Unlike the LLM
specialists in `specialists/`, each rule here:
  - Is a deterministic function `(candidate, ctx) -> Optional[Verdict]`
  - Costs ZERO API tokens (just code)
  - Is independently testable (no LLM, no API mocks)
  - Once written, works forever (assuming the signal it captures
    is real and the candidate fields it reads are stable)

The library target is 200 rules per docs/17. The AI's role shifts
from "decider" to "tie-breaker" as the library grows: most
candidates become unambiguous from the panel of rule verdicts,
and the LLM only resolves the genuinely-contested cases.

Output integration: `build_panel_block(candidate)` is called from
`ai_analyst._build_batch_prompt` and produces a compact text block
showing which rules fired and what they said. The LLM treats it
as another piece of context, weighed against its own judgment.

Adding a new rule:
  1. Drop a module under `deterministic_specialists/<rule_name>.py`
     exposing `NAME`, `DESCRIPTION`, `APPLIES_TO_SIGNALS` (tuple),
     and `evaluate(candidate, ctx) -> Optional[Verdict]`.
  2. Add the import to `RULE_MODULES` below.
  3. Add a focused test under `tests/test_deterministic_specialist_<name>.py`.

Rule severity convention:
  - VETO: rule has high confidence the trade should NOT happen
  - CAUTION: rule sees a yellow flag — does not stop the trade,
    but the AI should weigh it
  - CONFIRM: rule's pattern actively supports the candidate signal
  - (no return): rule had no view on this candidate
"""
from __future__ import annotations

import importlib
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Module paths importable as `deterministic_specialists.<X>`.
# Adding a new rule? Drop the module in this directory and add it
# to this list. Order is preserved in the rendered prompt block so
# group related rules together.
RULE_MODULES = [
    # ── Late-stage / extended pattern warnings (VETO/CAUTION LONG) ──
    "deterministic_specialists.rsi_overbought_late_stage",
    "deterministic_specialists.parabolic_blow_off",
    "deterministic_specialists.gap_into_resistance",
    "deterministic_specialists.bearish_divergence",
    "deterministic_specialists.extended_above_vwap",
    "deterministic_specialists.mfi_overbought_caution",
    "deterministic_specialists.cmf_distribution_long",
    # ── Breakout / momentum quality checks ──
    "deterministic_specialists.volume_dry_breakout",
    "deterministic_specialists.low_atr_breakout",
    "deterministic_specialists.weak_adx_breakout",
    # ── Smart-money + crowding (cautions) ──
    "deterministic_specialists.insider_sold_recently",
    "deterministic_specialists.high_short_interest_long",
    "deterministic_specialists.crowded_long",
    "deterministic_specialists.stocktwits_extreme_bullish",
    "deterministic_specialists.finra_short_volume_elevated",
    # ── Smart-money + flow (confirms) ──
    "deterministic_specialists.insider_cluster_buying",
    "deterministic_specialists.activist_13d_filed",
    "deterministic_specialists.dark_pool_accumulation",
    "deterministic_specialists.congressional_buying",
    "deterministic_specialists.unusual_options_activity",
    "deterministic_specialists.stocktwits_extreme_bearish",
    # ── Earnings / analyst momentum ──
    "deterministic_specialists.positive_earnings_revisions",
    "deterministic_specialists.negative_earnings_revisions",
    "deterministic_specialists.earnings_surprise_streak",
    "deterministic_specialists.earnings_miss_streak",
    "deterministic_specialists.earnings_within_window",
    # ── Regulatory / corporate-event warnings ──
    "deterministic_specialists.recent_8k_negative_event",
    "deterministic_specialists.recent_8k_exec_departure",
    "deterministic_specialists.risk_factor_diff_added",
    "deterministic_specialists.fda_inspection_warning",
    "deterministic_specialists.nhtsa_recall_active",
    "deterministic_specialists.sec_alert_high_severity",
    # ── Trend / pattern confirmations ──
    "deterministic_specialists.strong_adx_trend_confirm",
    "deterministic_specialists.rsi_oversold_uptrend",
    "deterministic_specialists.high_volume_confirmation",
    "deterministic_specialists.sector_relative_strength_confirm",
    "deterministic_specialists.sector_weakness_caution",
    "deterministic_specialists.sector_downtrend_long",
    "deterministic_specialists.cmf_accumulation_long",
    "deterministic_specialists.mfi_oversold_confirm",
    "deterministic_specialists.near_fib_support",
    "deterministic_specialists.squeeze_release_setup",
    "deterministic_specialists.orb_breakout",
    # ── Short-side specific ──
    "deterministic_specialists.below_vwap_short_extended",
    "deterministic_specialists.borrow_cost_high_short",
    "deterministic_specialists.squeeze_risk_short",
    # ── Macro / volatility regime ──
    "deterministic_specialists.options_iv_extreme_high",
    "deterministic_specialists.macro_risk_off_cross_asset_vol",
    "deterministic_specialists.yield_curve_inverted",
    "deterministic_specialists.cboe_skew_extreme",
    # ── Execution / friction ──
    "deterministic_specialists.slippage_high_caution",
    "deterministic_specialists.news_volume_spike",
]


def discover_rules() -> List[Any]:
    """Import every registered rule module and return the live ones.
    Mirrors the LLM-specialist registry shape (`specialists.__init__`)
    so the two systems feel consistent."""
    out: List[Any] = []
    for mod_path in RULE_MODULES:
        try:
            mod = importlib.import_module(mod_path)
        except (ImportError, AttributeError, SyntaxError) as exc:
            logger.warning(
                "deterministic rule failed to import: %s: %s",
                mod_path, exc,
            )
            continue
        if callable(getattr(mod, "evaluate", None)):
            out.append(mod)
    return out


def run_panel(candidate: Dict[str, Any], ctx: Any = None) -> List[Dict[str, Any]]:
    """Run every registered rule against the candidate. Returns a list
    of fired verdicts (rules that returned None are filtered out).

    Each verdict is a dict: `{name, severity, reasoning}`.

    Per `feedback_no_silent_failures`, each rule's exceptions are
    logged but do not break the panel — one bad rule shouldn't
    silence the others.
    """
    signal = (candidate.get("signal") or "").upper()
    fired: List[Dict[str, Any]] = []
    for mod in discover_rules():
        applies = getattr(mod, "APPLIES_TO_SIGNALS", ())
        if applies and signal and signal not in applies:
            continue
        try:
            verdict = mod.evaluate(candidate, ctx)
        except Exception as exc:
            logger.debug(
                "deterministic rule %s raised: %s: %s",
                getattr(mod, "NAME", mod.__name__),
                type(exc).__name__, exc,
            )
            continue
        if not verdict:
            continue
        fired.append({
            "name": getattr(mod, "NAME", mod.__name__),
            "severity": verdict.get("severity", "CAUTION"),
            "reasoning": verdict.get("reasoning", ""),
        })
    return fired


def format_panel_for_prompt(verdicts: List[Dict[str, Any]]) -> str:
    """Render the fired verdicts as a compact AI-prompt block.
    Empty input returns empty string so callers can splice
    unconditionally.

    Severity ordering: VETO > CAUTION > CONFIRM — the AI sees
    veto-level concerns first since they're the strongest signal.
    """
    if not verdicts:
        return ""
    severity_order = {"VETO": 0, "CAUTION": 1, "CONFIRM": 2}
    ranked = sorted(
        verdicts, key=lambda v: severity_order.get(v["severity"], 9))
    lines = []
    for v in ranked:
        lines.append(f"  [{v['severity']}] {v['name']}: {v['reasoning']}")
    return "\n".join(lines)


def build_panel_block(candidate: Dict[str, Any], ctx: Any = None) -> str:
    """End-to-end: run + format. Returns the complete prompt block
    (with header) or empty string when no rules fired.

    The caller can splice the return value into the prompt without
    a conditional — empty string means "no deterministic signal."
    """
    verdicts = run_panel(candidate, ctx)
    if not verdicts:
        return ""
    sym = candidate.get("symbol", "this candidate")
    header = f"\nDETERMINISTIC RULE PANEL FOR {sym} ({len(verdicts)} rule(s) fired):\n"
    return header + format_panel_for_prompt(verdicts)
