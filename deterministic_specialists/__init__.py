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
    # ── 2026-05-18 second batch ──
    # Trend / momentum
    "deterministic_specialists.rsi_midline_bull",
    "deterministic_specialists.rsi_midline_bear",
    "deterministic_specialists.stoch_overbought",
    "deterministic_specialists.stoch_oversold",
    "deterministic_specialists.low_adx_no_trade",
    "deterministic_specialists.strong_uptrend_pullback",
    # Gap / open behavior
    "deterministic_specialists.gap_down_capitulation",
    "deterministic_specialists.extreme_gap_news",
    # VWAP relationship
    "deterministic_specialists.above_vwap_long_confirm",
    "deterministic_specialists.below_vwap_long_caution",
    # Microstructure
    "deterministic_specialists.penny_stock_caution",
    "deterministic_specialists.squeeze_unreleased",
    "deterministic_specialists.squeeze_then_release_buy",
    # Attention / sentiment
    "deterministic_specialists.google_trends_spike",
    "deterministic_specialists.wikipedia_attention_surge",
    "deterministic_specialists.app_store_ranking_jump",
    "deterministic_specialists.app_store_ranking_drop",
    # Smart-money quality
    "deterministic_specialists.star_manager_holding",
    "deterministic_specialists.insider_track_record_strong",
    "deterministic_specialists.insider_track_record_weak",
    "deterministic_specialists.insider_buying_near_earnings",
    "deterministic_specialists.insider_selling_near_earnings",
    "deterministic_specialists.short_squeeze_setup",
    # Catalysts / fundamentals
    "deterministic_specialists.biotech_milestone_upcoming",
    "deterministic_specialists.transcript_sentiment_bullish",
    "deterministic_specialists.transcript_sentiment_bearish",
    "deterministic_specialists.patent_velocity_strong",
    "deterministic_specialists.epa_osha_violations_present",
    "deterministic_specialists.pe_extreme_high",
    "deterministic_specialists.pe_value_zone",
    # Options
    "deterministic_specialists.options_iv_rich_for_sellers",
    "deterministic_specialists.options_iv_cheap_for_buyers",
    "deterministic_specialists.options_pcr_panic",
    "deterministic_specialists.options_pcr_complacent",
    # Macro
    "deterministic_specialists.macro_low_vol_riskon",
    "deterministic_specialists.cboe_skew_complacent",
    "deterministic_specialists.macro_yield_curve_steepening",
    # 8-K events
    "deterministic_specialists.recent_8k_acquisition",
    "deterministic_specialists.recent_8k_regulation_fd",
    "deterministic_specialists.recent_8k_earnings_release",
    # Calendar (CONFIRMs only — wall-clock CAUTIONs dropped
    # 2026-05-18 PM as low-value panel noise. monday_morning_open,
    # last_30_min_session, first_5_min_session, friday_close_caution
    # added no signal beyond "look at the clock"; the LLM already
    # knows what time it is.)
    "deterministic_specialists.end_of_quarter_window",
    "deterministic_specialists.turn_of_month_strength",
    # Catalyst-attribution
    "deterministic_specialists.no_news_low_attention",
    "deterministic_specialists.multi_signal_consensus",
    "deterministic_specialists.low_conviction_score",
    "deterministic_specialists.sector_high_short_volume",
    # ── 2026-05-18 third batch ──
    # Factor signals
    "deterministic_specialists.momentum_5d_strong_positive",
    "deterministic_specialists.momentum_5d_negative_long",
    "deterministic_specialists.low_vol_factor",
    "deterministic_specialists.high_vol_caution",
    "deterministic_specialists.quality_factor_long",
    # Oscillator confluence
    "deterministic_specialists.triple_overbought",
    "deterministic_specialists.triple_oversold",
    # Bollinger walks
    "deterministic_specialists.bollinger_walk_up",
    "deterministic_specialists.bollinger_walk_down",
    # Round-number psychology
    "deterministic_specialists.round_number_resistance",
    "deterministic_specialists.round_number_support",
    # Sentiment depth
    "deterministic_specialists.retail_panic_oversold",
    "deterministic_specialists.retail_euphoria_overbought",
    "deterministic_specialists.sentiment_divergence",
    "deterministic_specialists.stocktwits_data_absent",
    # Macro detail
    "deterministic_specialists.macro_oil_vol_high",
    "deterministic_specialists.macro_treasury_vol_high",
    "deterministic_specialists.macro_gold_vol_high",
    "deterministic_specialists.macro_treasury_low_riskon",
    # Short-side complements
    "deterministic_specialists.squeeze_release_with_volume_short",
    "deterministic_specialists.rsi_bull_short_caution",
    "deterministic_specialists.rsi_bear_short_confirm",
    "deterministic_specialists.value_short_warning",
    "deterministic_specialists.expensive_short_confirm",
    # Calendar (CONFIRMs only after 2026-05-18 PM noise cleanup)
    "deterministic_specialists.wednesday_strength",
    # Options flow detail
    "deterministic_specialists.options_unusual_calls",
    "deterministic_specialists.options_unusual_puts",
    "deterministic_specialists.options_iv_normal_zone",
    # Catalyst stacking
    "deterministic_specialists.multiple_negative_catalysts",
    "deterministic_specialists.multiple_positive_catalysts",
    "deterministic_specialists.divergent_signals_caution",
    # Execution / liquidity
    "deterministic_specialists.wide_spread_caution",
    "deterministic_specialists.extreme_high_price_caution",
    "deterministic_specialists.multi_alt_data_silent",
    # Volume / flow
    "deterministic_specialists.strong_volume_late_session",
    "deterministic_specialists.insider_recent_buys_meaningful",
    "deterministic_specialists.finra_short_volume_collapsed",
    "deterministic_specialists.cmf_neutral_low_signal",
    # Sector rotation
    "deterministic_specialists.sector_sector_rotation_signal",
    "deterministic_specialists.sector_sector_strength_aligned",
    # Compound signals
    "deterministic_specialists.squeeze_with_consensus",
    "deterministic_specialists.insider_cluster_with_options",
    # Intraday flow
    "deterministic_specialists.intraday_pattern_aligned",
    "deterministic_specialists.intraday_pattern_opposed",
    # Tax / cycle
    "deterministic_specialists.wash_cycle_recent",
    # ── 2026-05-18 PM: candlestick-proxy batch (Phase 3 final stretch) ──
    # Uses OHLC of the last 3 bars surfaced via trade_pipeline.
    # _get_latest_indicators → candidate["candle"]. Zero new API
    # calls; pure derived features from the existing 200-bar fetch.
    "deterministic_specialists.candle_hammer",
    "deterministic_specialists.candle_shooting_star",
    "deterministic_specialists.candle_doji",
    "deterministic_specialists.candle_bullish_engulfing",
    "deterministic_specialists.candle_bearish_engulfing",
    "deterministic_specialists.candle_inside_day",
    "deterministic_specialists.candle_outside_day",
    "deterministic_specialists.candle_marubozu_long",
    "deterministic_specialists.candle_marubozu_short",
    "deterministic_specialists.candle_three_white_soldiers",
    "deterministic_specialists.candle_three_black_crows",
    "deterministic_specialists.candle_hanging_man",
    "deterministic_specialists.candle_piercing_pattern",
    "deterministic_specialists.candle_dark_cloud_cover",
    "deterministic_specialists.candle_morning_star",
    "deterministic_specialists.candle_evening_star",
    # ── 2026-05-18 PM: market-context + portfolio batch ──
    # Reads `candidate["_market_context"]` (regime, vix, spy_trend,
    # sector_rotation, crisis_context, macro_event_block) and
    # `candidate["_portfolio"]` (positions, drawdown_pct) — both
    # stashed by ai_analyst._build_batch_prompt right before the
    # panel runs. Zero new data fetches; pure plumbing of context
    # already built upstream.
    "deterministic_specialists.regime_bullish_long_confirm",
    "deterministic_specialists.regime_bearish_long_caution",
    "deterministic_specialists.regime_bearish_short_confirm",
    "deterministic_specialists.regime_bullish_short_caution",
    "deterministic_specialists.regime_volatile_caution",
    "deterministic_specialists.vix_high_caution",
    "deterministic_specialists.vix_extreme_panic",
    "deterministic_specialists.vix_low_riskon",
    "deterministic_specialists.vix_extreme_complacency",
    "deterministic_specialists.spy_uptrend_long_confirm",
    "deterministic_specialists.spy_downtrend_long_caution",
    "deterministic_specialists.spy_downtrend_short_confirm",
    "deterministic_specialists.crisis_state_long_caution",
    "deterministic_specialists.crisis_state_short_confirm",
    "deterministic_specialists.macro_event_imminent",
    "deterministic_specialists.sector_rotation_top_winner",
    "deterministic_specialists.sector_rotation_bottom_loser",
    "deterministic_specialists.portfolio_already_long",
    "deterministic_specialists.portfolio_already_short",
    "deterministic_specialists.portfolio_high_drawdown",
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


# 2026-05-19 (Phase B2): directional translation so the 179
# deterministic rules — written against stock actions
# (BUY/SELL/SHORT) — ALSO fire on directionally-equivalent
# OPTIONS / MULTILEG_OPEN candidates.
#
# Before: every rule's `APPLIES_TO_SIGNALS` enumerated stock
# actions, so an OPTIONS or MULTILEG_OPEN candidate matched
# nothing and skipped the entire 179-rule panel — option
# proposals got ~3-8 LLM specialists' verdicts vs ~150+ for stock
# proposals on the same underlying. Per memory rule "[System
# exists to trade and make money, not hoard cash]", under-
# covering option proposals = weaker pre-trade gate = either
# more bad option trades AND/OR more options being unnecessarily
# rejected by the AI for lack of confirmation.
#
# After: the router computes the candidate's DIRECTION from
# (action, option_strategy) and matches a rule's stock-action
# `APPLIES_TO_SIGNALS` against that direction. A rule listing
# `BUY/STRONG_BUY/WEAK_BUY` (bullish stock) now also fires on
# bullish options (`long_call`, `bull_call_spread`, etc.). Zero
# rule files needed to change.

BULLISH_STOCK_ACTIONS = frozenset({"BUY", "STRONG_BUY", "WEAK_BUY"})
BEARISH_STOCK_ACTIONS = frozenset({"SELL", "STRONG_SELL", "WEAK_SELL", "SHORT"})

# Bullish option strategies: profit when the underlying rises
# (or stays above strike for collected-premium strategies).
BULLISH_OPTION_STRATEGIES = frozenset({
    "long_call",
    "bull_call_spread",
    "bull_put_spread",
    "cash_secured_put",
    "covered_call",
})
# Bearish option strategies: profit when the underlying falls
# (or stays below strike for short-call strategies).
BEARISH_OPTION_STRATEGIES = frozenset({
    "long_put",
    "bear_call_spread",
    "bear_put_spread",
    "protective_put",
})
# Non-directional strategies (range-bound or vol-bet) intentionally
# don't trigger directional rules — they have their own option-
# specific specialists (`gamma_pin_specialist`, `iv_skew_specialist`,
# `option_spread_risk`) that cover the structural risks.
_NEUTRAL_OPTION_STRATEGIES = frozenset({
    "iron_condor",
    "iron_butterfly",
    "straddle",
    "strangle",
    "calendar_spread",
})


def signal_direction(candidate: Dict[str, Any],
                      ) -> Optional[str]:
    """Return 'bullish' / 'bearish' / 'neutral' / None for a
    candidate based on its signal + option_strategy.

    Used by `run_panel` to translate OPTIONS / MULTILEG_OPEN
    candidates into a direction so directional rules fire on them.
    Stock-side actions translate directly. Unknown signals return
    None (no rule fires).
    """
    signal = (candidate.get("signal") or "").upper()
    if signal in BULLISH_STOCK_ACTIONS:
        return "bullish"
    if signal in BEARISH_STOCK_ACTIONS:
        return "bearish"
    if signal in ("OPTIONS", "MULTILEG_OPEN"):
        strat = (candidate.get("option_strategy") or "").lower()
        if strat in BULLISH_OPTION_STRATEGIES:
            return "bullish"
        if strat in BEARISH_OPTION_STRATEGIES:
            return "bearish"
        if strat in _NEUTRAL_OPTION_STRATEGIES:
            return "neutral"
        # Unknown option_strategy on an OPTIONS/MULTILEG_OPEN
        # candidate — don't fire directional rules (they may
        # mis-attribute). The option-specific specialists still
        # run via the LLM-narrative ensemble.
        return None
    return None


def _rule_matches_directional_candidate(applies: tuple,
                                         direction: Optional[str]) -> bool:
    """A rule whose APPLIES_TO_SIGNALS lists stock actions
    `applies` matches a directional (options/multileg) candidate
    when its enumerated actions overlap with the same-direction
    stock-action set."""
    if direction is None or direction == "neutral":
        return False
    applies_set = set(applies)
    if direction == "bullish":
        return bool(applies_set & BULLISH_STOCK_ACTIONS)
    if direction == "bearish":
        return bool(applies_set & BEARISH_STOCK_ACTIONS)
    return False


def run_panel(candidate: Dict[str, Any], ctx: Any = None) -> List[Dict[str, Any]]:
    """Run every registered rule against the candidate. Returns a list
    of fired verdicts (rules that returned None are filtered out).

    Each verdict is a dict: `{name, severity, reasoning}`.

    Per `feedback_no_silent_failures`, each rule's exceptions are
    logged but do not break the panel — one bad rule shouldn't
    silence the others.

    Routing (2026-05-19): for stock-side actions, the rule's
    `APPLIES_TO_SIGNALS` tuple is matched directly (legacy
    behavior). For OPTIONS / MULTILEG_OPEN candidates, the
    candidate's direction (bullish/bearish via option_strategy
    lookup) is computed and the rule fires if its actions overlap
    the same-direction stock-action set. Lets the 179-rule
    library serve options proposals without per-rule edits.
    """
    signal = (candidate.get("signal") or "").upper()
    direction = signal_direction(candidate) if signal in ("OPTIONS",
                                                             "MULTILEG_OPEN") else None
    fired: List[Dict[str, Any]] = []
    for mod in discover_rules():
        applies = getattr(mod, "APPLIES_TO_SIGNALS", ())
        if applies:
            # Direct stock-signal match (legacy path)
            direct_match = signal and signal in applies
            # New: directional translation for options/multileg
            directional_match = (
                direction is not None
                and _rule_matches_directional_candidate(applies, direction)
            )
            if not (direct_match or directional_match):
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

    2026-07-02 (token review): CONFIRM verdicts render as a NAME LIST, not
    full prose. Measured: the panel was 63% of every candidate's prompt
    block, and CONFIRM reasoning was largely verbatim repetition of facts
    already in the candidate's own data lines (dark-pool shares,
    earnings-beat streak, P/C ratio, insider lines...) — duplicated text
    competing for the selector's attention. VETO/CAUTION reasoning stays
    verbatim (the risk detail IS the signal). RENDER-ONLY change: the
    structured verdicts (rule_votes_json for the fine-tune corpus, the
    weight-tuner's predicates) are untouched — data purity intact."""
    if not verdicts:
        return ""
    severity_order = {"VETO": 0, "CAUTION": 1, "CONFIRM": 2}
    ranked = sorted(
        verdicts, key=lambda v: severity_order.get(v["severity"], 9))
    lines = []
    confirms = []
    for v in ranked:
        if v.get("severity") == "CONFIRM":
            confirms.append(v.get("name", "?"))
        else:
            lines.append(f"  [{v['severity']}] {v['name']}: {v['reasoning']}")
    if confirms:
        lines.append(f"  [CONFIRM x{len(confirms)}] {', '.join(confirms)}")
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
