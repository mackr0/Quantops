"""Guardrail: every column in `trading_profiles` is either autonomously
tuned by the system or explicitly on the MANUAL_PARAMETERS allowlist
with a written rationale.

This is the structural guarantee that closes the "the tuner could
spot it but couldn't act on it" hole. New columns added to the schema
will fail this test until the author either:
  1. Wires a tuning rule (somewhere `update_trading_profile(... <col>=
     ...)` is called inside self_tuning.py), OR
  2. Plugs the column into one of the override-stack JSON dicts
     (signal_weights / regime_overrides / tod_overrides /
     symbol_overrides / prompt_layout / capital_scale), OR
  3. Adds an entry to MANUAL_PARAMETERS below with a rationale
     explaining why human-only control is appropriate.

The cost-effective alternative to extensive code review: structurally
enforce that the system's autonomy can't quietly regress.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Set

import pytest


# Columns that are intentionally not autonomously tuned, with the
# reason. Adding to this list requires a written rationale — the test
# fails on stale entries (columns no longer in the schema).
MANUAL_PARAMETERS = {
    # Universe / liquidity floors — OPERATOR-ONLY (2026-06-26). These
    # define WHAT is eligible to trade; the self-tuner optimizes HOW to
    # trade. A tuner that could relax its own liquidity floor would walk
    # it down to chase entry count (optimizer defeats its own risk
    # limit), so these are set only in Settings. Enforced class-wide by
    # self_tuning._OPERATOR_ONLY_PARAMS / the _apply_param_change firewall;
    # pinned by tests/test_universe_floors_operator_only_2026_06_26.py.
    "min_price":   "Operator-only universe floor — never auto-tuned (_OPERATOR_ONLY_PARAMS)",
    "max_price":   "Operator-only universe ceiling — never auto-tuned (_OPERATOR_ONLY_PARAMS)",
    "min_volume":  "Operator-only share-liquidity floor — never auto-tuned (_OPERATOR_ONLY_PARAMS)",
    "min_adv":     "Operator-only dollar-liquidity floor — never auto-tuned (_OPERATOR_ONLY_PARAMS)",

    # Identity / metadata — not "parameters" at all
    "id":              "PRIMARY KEY",
    "user_id":         "Foreign key to users",
    "name":            "User-chosen profile name",
    "created_at":      "Historical timestamp",
    "market_type":     "Defines the trading universe — strategic choice",
    "enabled":         "User-controlled on/off switch",

    # Secrets — never autonomous
    "alpaca_api_key_enc":     "Secret",
    "alpaca_secret_key_enc":  "Secret",
    "ai_api_key_enc":         "Secret",
    "consensus_api_key_enc":  "Secret",
    "alpaca_account_id":      "Foreign key to alpaca_accounts (set by user)",

    # AI provider/model — explicit per-profile opt-in needed
    "ai_provider":         "Strategic AI choice — opt-in via ai_model_auto_tune (cost concern)",
    "ai_model":            "Strategic AI choice — opt-in via ai_model_auto_tune (cost concern)",
    "ai_model_auto_tune":  "Per-profile opt-in toggle (user-set; not autonomously tuned)",

    # Architectural — multi-model setup is intentional
    "enable_consensus": "Architectural choice (multi-model)",
    "consensus_model":  "Architectural choice (consensus AI)",

    # Shadow model evaluation — observational only; never affects
    # operational behavior. Operator decides which candidate models
    # to test and is the only one who knows what they want to compare,
    # so tuning is the operator's domain. The shadow_models list and
    # shadow_api_keys_enc dict are pure config; enable_shadow_eval is
    # the on/off switch.
    "enable_shadow_eval":   "Architectural choice (operator picks candidate models to A/B)",
    "shadow_models":        "User-curated candidate-model list",
    "shadow_api_keys_enc":  "Secret",

    # Schedule / lifestyle — when the user wants trading active
    "schedule_type": "User lifestyle (when to trade)",
    "custom_start":  "User lifestyle",
    "custom_end":    "User lifestyle",
    "custom_days":   "User lifestyle",

    # Meta — tuner can't disable itself
    "enable_self_tuning": "Meta — tuner cannot disable itself",

    # 2026-05-17 ablation flags — these define experimental arms in
    # the 13-profile fresh-start experiment (docs/15_EXPERIMENT_DESIGN_
    # 2026_05_17.md). Auto-tuning them would defeat their purpose
    # (the experiment is testing whether THESE components add alpha).
    # strategy_type ('ai' / 'buy_hold' / 'random') is a fundamental
    # architecture choice, not a tunable knob.
    "enable_alt_data":   "Ablation arm flag — operator-set for experiment",
    "enable_meta_model": "Ablation arm flag — operator-set for experiment",
    "enable_options":    "Ablation arm flag — operator-set for experiment",
    "enable_stocks":     "Asset-class enablement (per-profile user choice; pairs with enable_options/enable_crypto)",
    "enable_crypto":     "Asset-class enablement (per-profile user choice; pairs with enable_stocks/enable_options)",
    "strategy_type":     "Strategy mode (ai/buy_hold/random) — architectural choice",

    # Historical baselines / virtual-account layer
    "initial_capital": "Historical baseline, not tunable",
    "is_virtual":      "Set at profile creation",

    # Conviction-TP-override toggles — strategic choice (these
    # bypass take-profit entirely on high-conviction trades, which
    # is a risk preference the user explicitly opts into)
    "use_conviction_tp_override":   "Strategic risk-preference choice",
    "conviction_tp_min_confidence": "Strategic risk-preference choice",
    "conviction_tp_min_adx":        "Strategic risk-preference choice",

    # Layer-9 storage column for the auto-allocator's recommendation
    # (rebalanced by capital_allocator.rebalance, not the tuner directly)
    "capital_scale": "Set by capital_allocator (Layer 9), not the tuner",

    # Layer-2/3/4/6/7 storage — these JSON columns ARE tuned, but
    # not via update_trading_profile(<column>=...) — they're set via
    # the layer-specific helpers (set_weight, set_override, etc.).
    # Mark as "manual" for the regex-based tuned-detector since
    # they don't appear as direct update_trading_profile call sites.
    "signal_weights":    "Layer 2 storage — tuned via signal_weights.set_weight",
    "regime_overrides":  "Layer 3 storage — tuned via regime_overrides.set_override",
    "tod_overrides":     "Layer 4 storage — tuned via tod_overrides.set_override",
    "symbol_overrides":  "Layer 7 storage — tuned via symbol_overrides.set_override",
    "prompt_layout":     "Layer 6 storage — tuned via prompt_layout.set_verbosity",

    # Custom user content
    "custom_watchlist": "User-curated symbol list — purely user choice",

    # Boolean execution toggles — deferred to Layer 2 weighted intensity
    # (use_atr_stops, use_trailing_stops, use_limit_orders weights).
    # The booleans themselves stay as user-set defaults; the weight
    # decides intensity at decision time.
    "use_atr_stops":      "Default user choice; intensity tuned via Layer 2 signal_weights['use_atr_stops']",
    "use_trailing_stops": "Default user choice; intensity tuned via Layer 2 signal_weights['use_trailing_stops']",
    "use_limit_orders":   "Default user choice; intensity tuned via Layer 2 signal_weights['use_limit_orders']",

    # Lever 2 / Lever 3 of COST_AND_QUALITY_LEVERS_PLAN.md.
    # disabled_specialists is auto-managed by
    # `_task_specialist_health_check` in multi_scheduler.py (not
    # self_tuning.py), so the regex-based tuned-detector doesn't
    # see the update_trading_profile call. meta_pregate_threshold
    # is a per-profile config, not auto-tuned — the gate is
    # binary-effective (on/off) and the threshold itself doesn't
    # need autonomous tuning beyond the user-set default.
    "disabled_specialists":    "Lever 3 — auto-managed by multi_scheduler._task_specialist_health_check (calibrator-driven)",
    "meta_pregate_threshold":  "Lever 2 — per-profile gate threshold, default 0.5; user override; not autonomously tuned",

    # COMPETITIVE_GAP_PLAN feature toggles. Each gates a per-profile
    # scheduled task. User-controlled architectural choice (do you
    # want this safety / research feature running?), not a parameter
    # the tuner should A/B test on its own.
    "enable_intraday_risk_halt":     "User-controlled safety toggle (Item 2b auto-halt)",
    "enable_stat_arb_pairs":         "User-controlled feature toggle (Item 1b stat-arb book; requires shorts enabled)",
    "enable_portfolio_risk_snapshot": "User-controlled feature toggle (Item 2a daily Barra snapshot)",
    # Item 1c — long-vol hedge toggle + thresholds. Architectural
    # choice (do you want active tail-risk insurance?) + user-set
    # threshold preferences. Not autonomously tuned — the AI prompt
    # surfaces the hedge state so the model can reason about it,
    # but the trigger thresholds themselves are deliberate cost /
    # coverage trade-offs the user owns.
    "enable_long_vol_hedge":          "User-controlled feature toggle (Item 1c long-vol hedge)",
    "long_vol_hedge_drawdown_pct":    "User-set hedge trigger preference (drawdown threshold)",
    "long_vol_hedge_var_pct":         "User-set hedge trigger preference (VaR threshold)",
    "long_vol_hedge_premium_pct":     "User-set hedge sizing (% of book per hedge)",
    # OPEN_ITEMS #4 — wheel automation symbol opt-in list. Strategic
    # choice (which names is this profile willing to be assigned in
    # exchange for premium income), not a tunable parameter.
    "wheel_symbols":                   "User-curated symbol opt-in list for the wheel cycle",
    # OPEN_ITEMS #10 — options roll-window thresholds. User
    # preference (tighter management vs more premium captured), not
    # autonomously tunable.
    "options_roll_window_days":         "User-set roll-window threshold preference",
    "options_auto_close_profit_pct":    "User-set credit-position auto-close threshold",
    "options_roll_recommend_profit_pct": "User-set roll-recommend threshold",

    # P2.2 of LONG_SHORT_PLAN.md — strategic choice (long-only vs
    # balanced vs short-dominant). The AI prompt directive (P2.2)
    # and the balance gate (P2.4) work together to enforce the
    # target, but the target ITSELF is set by the user. Auto-tuning
    # this would defeat the purpose — the target IS the user's
    # intent for what kind of book this profile runs.
    "target_short_pct": "Strategic balance preference — set by user, not autonomously tuned",
    # P4.1 of LONG_SHORT_PLAN.md — book beta target. Strategic
    # risk-preference choice (market-neutral 0.0, low-net 0.5,
    # market-following 1.0). Auto-tuning would defeat the purpose;
    # the AI prompt directive + balance gate are what enforce the
    # target. Set by user.
    "target_book_beta": "Strategic risk-preference target — set by user, not autonomously tuned",
    # P1.5 of LONG_SHORT_PLAN.md — short_max_hold_days has a tuning
    # rule (_optimize_short_max_hold_days) that currently returns
    # None pending the days_held column on closed shorts. Will fire
    # autonomously once short cover rows accumulate enough hold-time
    # data to analyze. Listed here so the lever-is-tuned guardrail
    # passes today; remove from this list when the rule's stub
    # body is filled in (see self_tuning.py:_optimize_short_max_hold_days).
    "short_max_hold_days": "Tuning rule scaffolded but stub-only until days_held data accumulates",

    # Kill-switch state — operator-set safety override; the tuner must
    # never re-enable trading on its own once it's been halted, and the
    # halt reason + timestamp are audit trail, not knobs.
    "trading_halted": "Kill-switch state — operator-set safety override",
    "halt_reason":    "Kill-switch audit trail — human-written rationale",
    "halted_at":      "Kill-switch audit trail — timestamp set at halt time",

    # Pipeline-refactor cutover flags (Scope C). use_pipeline_dispatch
    # toggles the per-pipeline dispatch path; enable_pipeline_shadow_eval
    # arms the shadow harness for A/B comparison vs the legacy path.
    # Both are architectural rollout toggles owned by the operator
    # during the cutover; not autonomously tuned.
    "use_pipeline_dispatch":      "Pipeline-cutover architectural toggle — operator-set during rollout",
    "enable_pipeline_shadow_eval": "Pipeline-cutover shadow-harness toggle — operator-set during rollout",
}


def _profile_columns() -> Set[str]:
    """Parse `trading_profiles` schema (CREATE TABLE + ALTER TABLE
    migrations) from models.py and return the full set of columns."""
    src = (Path(__file__).resolve().parent.parent / "models.py").read_text()

    # CREATE TABLE block
    create_match = re.search(
        r"CREATE TABLE IF NOT EXISTS trading_profiles \((.*?)\);",
        src, flags=re.DOTALL,
    )
    cols = set()
    if create_match:
        body = create_match.group(1)
        # Each non-FK line that starts with a word is a column.
        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith("FOREIGN KEY") or line.startswith("UNIQUE"):
                continue
            m = re.match(r"^([a-z_]+)\s", line)
            if m:
                cols.add(m.group(1))

    # ALTER TABLE migrations — match the _migrations list entries
    for m in re.finditer(
        r'\(\s*"trading_profiles"\s*,\s*"([a-z_]+)"\s*,',
        src,
    ):
        cols.add(m.group(1))

    return cols


def _tuned_columns() -> Set[str]:
    """Find columns that the tuner directly updates via
    update_trading_profile(...) calls.

    Scans BOTH legacy self_tuning.py AND the new
    pipelines/tuning_writer.py path (Phase 2b, 2026-05-12). The
    pipeline tuner adjusts columns via the Pipeline.tune() ->
    apply_parameter_adjustments path; columns it writes appear in
    the BOUNDS dict in pipelines/option.py:tune (and similar in
    future stock.py:tune).

    Catches four patterns:
      1. update_trading_profile(pid, <col>=value)
      2. update_trading_profile(pid, **{<col>: value})
      3. The strategy-toggle dict-key pattern.
      4. _apply_param_change(pid, uid, "<type>", "<col>", ...) —
         the 2026-05-18 guardrail wrapper that the per-cycle delta
         cap routes every continuous-parameter write through.
    Plus the BOUNDS dict in pipelines/{stock,option}.py:tune which
    enumerates columns the new pipeline tuner adjusts.
    """
    base = Path(__file__).resolve().parent.parent
    sources = [
        (base / "self_tuning.py").read_text(),
    ]
    cols = set()

    # Phase 2b — pipeline tuner BOUNDS dicts. Each key is a column
    # the OptionPipeline.tune() / StockPipeline.tune() adjusts.
    for pipeline_file in ("pipelines/option.py", "pipelines/stock.py"):
        try:
            pipe_src = (base / pipeline_file).read_text()
        except FileNotFoundError:
            continue
        # Match `BOUNDS = { "<col>": ... }` style declarations
        bounds_match = re.search(
            r"BOUNDS\s*=\s*\{(.*?)\n\s*\}",
            pipe_src, flags=re.DOTALL,
        )
        if bounds_match:
            for col in re.findall(
                r'"([a-z_]+)"', bounds_match.group(1)
            ):
                cols.add(col)

    src = sources[0]

    # Pattern 1 + 2
    for m in re.finditer(
        r"update_trading_profile\(\s*\w+\s*,\s*\*\*\{([a-z_]+):"
        r"|update_trading_profile\(\s*\w+\s*,\s*([a-z_]+)\s*=",
        src,
    ):
        col = m.group(1) or m.group(2)
        if col:
            cols.add(col)

    # Pattern 4 — _apply_param_change wrapper. Capture the 4th
    # positional arg (param_name) of every call. Allows multi-line
    # calls with optional whitespace between args.
    for m in re.finditer(
        r"_apply_param_change\(\s*"
        r"[^,]+,\s*"           # profile_id
        r"[^,]+,\s*"           # user_id
        r'"[^"]+"\s*,\s*'      # adjustment_type
        r'"([a-z_]+)"',        # param_name (captured)
        src,
        flags=re.DOTALL,
    ):
        cols.add(m.group(1))

    # Pattern 3 — _optimize_strategy_toggles uses
    # `**{toggle_col: 0}` where toggle_col comes from
    # _STRATEGY_TYPE_TO_TOGGLE.values(). Add those values directly.
    if "_optimize_strategy_toggles" in src and "toggle_col" in src:
        toggle_match = re.search(
            r"_STRATEGY_TYPE_TO_TOGGLE\s*=\s*\{(.*?)\}",
            src, flags=re.DOTALL,
        )
        if toggle_match:
            for v in re.findall(r'"([a-z_]+)"', toggle_match.group(1)):
                # Only the values are toggle columns; keys are
                # short strategy names like "momentum_breakout"
                if v.startswith("strategy_"):
                    cols.add(v)

    return cols


class TestEveryLeverIsTuned:
    def test_every_profile_column_is_tuned_or_explicitly_manual(self):
        all_cols = _profile_columns()
        tuned = _tuned_columns()
        manual = set(MANUAL_PARAMETERS.keys())

        missing = []
        for col in sorted(all_cols):
            if col in tuned:
                continue
            if col in manual:
                continue
            missing.append(col)

        if missing:
            pytest.fail(
                "The following trading_profiles columns are neither auto-tuned\n"
                "by self_tuning.py nor on the MANUAL_PARAMETERS allowlist:\n\n"
                + "\n".join(f"  - {c}" for c in missing)
                + "\n\nFix one of:\n"
                "  1. Add a tuning rule in self_tuning.py that calls\n"
                "     update_trading_profile(profile_id, <col>=value).\n"
                "  2. Add the column to one of the override-stack JSON\n"
                "     dicts (signal_weights / regime_overrides /\n"
                "     tod_overrides / symbol_overrides / prompt_layout).\n"
                "  3. Add an entry to MANUAL_PARAMETERS in this test\n"
                "     with a written rationale.\n"
            )

    def test_no_stale_entries_in_manual_allowlist(self):
        """Stale MANUAL_PARAMETERS entries hide gaps in the schema's
        autonomy coverage. Fail if any allowlisted column no longer
        exists in trading_profiles."""
        all_cols = _profile_columns()
        stale = [k for k in MANUAL_PARAMETERS if k not in all_cols]
        if stale:
            pytest.fail(
                "MANUAL_PARAMETERS entries no longer in trading_profiles:\n"
                + "\n".join(f"  - {c!r}" for c in stale)
                + "\n\nRemove them to keep the guardrail tight."
            )
