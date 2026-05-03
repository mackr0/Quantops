# 11 — Integration Guide

**Audience:** developers extending the platform with new strategies, signals, specialists, scheduled features, or schema columns.
**Purpose:** procedural how-to reference. Each section below is a recipe — follow it end-to-end and the guardrail tests pass.
**Last updated:** 2026-05-03.

This guide is procedural. The principles those procedures encode are in `docs/10_METHODOLOGY.md`.

## 1. Adding a new strategy

A strategy is a pure function that emits a vote per symbol per cycle.

### 1a. Steps

1. **Create file:** `strategies/<strategy_name>.py`. Follow the convention of existing strategies — start by reading `strategies/momentum_breakout.py` as a template.
2. **Define `run(symbol, market_type, df, params=None) → dict`**. Returns:
   ```python
   {
     "signal": "STRONG_BUY" | "BUY" | "HOLD" | "SELL" | "STRONG_SELL" | "SHORT" | "STRONG_SHORT",
     "score": float (0-100),
     "reason": str,
     # Optional: any extra fields you want surfaced to the AI prompt
   }
   ```
3. **Register the strategy** by adding it to `multi_strategy.STRATEGY_REGISTRY` with its market-type applicability. (Or for auto-applicability, ensure the strategy file is importable from `strategies/`.)
4. **Run the rigorous backtest gauntlet** (`rigorous_backtest.py`) to validate the strategy passes the 10 gates. Strategies that fail must NOT ship live.
5. **If the strategy's vote should be tunable per-profile (Layer 2):** add an entry to `signal_weights.WEIGHTABLE_SIGNALS`:
   ```python
   ("vote_<strategy_name>", "Strategy: <Display Name>",
       lambda f: f.get("vote_<strategy_name>") in ("BUY", "STRONG_BUY", "SHORT", "STRONG_SHORT"))
   ```
6. **Run `pytest tests/`**. The relevant guardrail tests:
   - `test_seed_strategies` — every strategy must register cleanly.
   - `test_meta_features_have_ui` — if a feature was added to support the strategy.

### 1b. Edge cases

- **Strategy that uses options data:** consume from `options_oracle.get_options_oracle(symbol)` rather than fetching directly.
- **Strategy that uses alt-data:** consume from the relevant `alternative_data.get_*` helper. Don't add network calls.
- **Strategy that's slow:** every strategy runs on every symbol. If the strategy is O(n²) or makes API calls, it will balloon the cycle time. Profile before adding.
- **Bearish strategies:** must have `enable_short_selling=1` in the profile; otherwise their votes are ignored.

## 2. Adding a new alt-data signal

A signal is a per-symbol feature (numeric or categorical) the AI sees and the meta-model trains on.

### 2a. Steps

1. **Add a fetcher** in `alternative_data.py`:
   ```python
   def get_<signal_name>(symbol: str) -> Dict[str, Any]:
       """Docstring with output shape + cache TTL."""
       cache_key = f"<signal_name>_{symbol.upper()}"
       cached = _get_cached(cache_key, "<ttl_class>")
       if cached is not None:
           return cached

       result = {"<key>": None, "has_data": False}
       try:
           # ... fetch logic ...
           result.update({"<key>": value, "has_data": True})
       except Exception as exc:
           logging.debug("<signal_name> fetch failed for %s: %s", symbol, exc)

       _set_cached(cache_key, result)
       return result
   ```
2. **Register a cache TTL:** add `_CACHE_TTL["<ttl_class>"] = <seconds>` near the bottom of `alternative_data.py`.
3. **Wire into the aggregator:** add `"<signal_name>": get_<signal_name>(symbol)` to `get_all_alternative_data`.
4. **If numeric:** add to `meta_model.NUMERIC_FEATURES`. Add a flatten step in `trade_pipeline._build_features_payload` so the meta-model trains on it.
5. **If categorical:** add to `meta_model.CATEGORICAL_FEATURES` with the value list. Same flatten step.
6. **If user-tunable:** add an entry to `signal_weights.WEIGHTABLE_SIGNALS`.
7. **Render in AI prompt:** add a `_weighted_signal_text("<signal_name>", "<rendered>")` block in `ai_analyst._build_alt_data_section`. Read existing blocks (e.g. for `congressional_recent`) as templates.
8. **Add display name** to `display_names._DISPLAY_NAMES` for the signal name and any underlying identifiers.
9. **Add tests:** mock the upstream API; verify graceful failure when source is unavailable.

### 2b. Guardrails that fire

- `test_meta_features_have_ui` — fails if you added a feature without rendering it.
- `test_no_snake_case_in_user_facing_ids` — fails if you have any new identifiers that need display name entries.
- `test_alternative_data_new` — fails if `get_all_alternative_data` doesn't wire your signal.

### 2c. Edge cases

- **Crypto symbols:** signals that don't apply to crypto should return `{"is_crypto": True, "has_data": False}` early.
- **Rate-limited APIs:** cache aggressively. If the upstream rate-limits, return graceful failure rather than raising.
- **Daily-snapshot signals:** if your signal needs daily history (like App Store WoW), add a daily scheduler task per the recipe in §4.

## 3. Adding a new schema column on `trading_profiles`

A schema column is a per-profile setting.

### 3a. Steps

1. **Add to migrations** in `models.py`'s `_migrations` list:
   ```python
   ("trading_profiles", "<column_name>", "REAL NOT NULL DEFAULT <value>"),
   ```
2. **Add to `update_trading_profile`'s `allowed_cols` set** in `models.py`.
3. **Add to `UserContext`** in `user_context.py` as a dataclass field with a default.
4. **Add to `build_user_context_from_profile`** in `models.py`:
   ```python
   <column_name>=profile.get("<column_name>", <default>),
   ```
5. **Add a settings UI control** in `templates/settings.html`. Use existing fields as templates.
6. **Add to `save_profile` form parser** in `views.py`:
   ```python
   config_updates["<column_name>"] = <type>(form.get("<column_name>", <default>))
   ```
7. **Add to `views.PARAM_DISPLAY_NAMES`** for the display label.
8. **Either:**
   - **Add a tuning rule** in `self_tuning.py` (call `update_trading_profile(profile_id, <col>=value)` from a `_optimize_*` function), OR
   - **Add to `MANUAL_PARAMETERS`** in `tests/test_every_lever_is_tuned.py` with a written rationale.

### 3b. Guardrails that fire

- `test_every_lever_is_tuned` — fails if a column is neither auto-tuned nor on `MANUAL_PARAMETERS`.
- `test_ctx_field_round_trip` — fails if code calls `getattr(ctx, "<column_name>", ...)` but the column isn't on `UserContext`.
- Settings page schema sanity test — fails if the migration breaks an existing test profile.

## 4. Adding a new scheduled task

A scheduled task is a per-cycle or once-per-day function in `multi_scheduler.py`.

### 4a. Steps

1. **Define the task** as a top-level function:
   ```python
   def _task_<name>(ctx):
       """Docstring."""
       seg_label = ctx.display_name or ctx.segment
       try:
           # ... task logic ...
           logging.info(f"[{seg_label}] <task name>: ...")
       except Exception:
           logging.exception(f"[{seg_label}] <task name> failed")
   ```
2. **Register the task** in `run_segment_cycle`:
   ```python
   if getattr(ctx, "enable_<feature>", <default>):
       run_task(
           f"[{seg_label}] <Task Display Name>",
           lambda: _task_<name>(ctx),
           db_path=ctx.db_path,
       )
   ```
3. **If the task is once-per-day**, use the marker pattern (read `_task_app_store_snapshot` as a template). Marker tables live in the master DB.
4. **Either:**
   - **Add an `enable_<feature>` schema column** (per §3 above) — gate the task on it, OR
   - **Add the task to `INFRASTRUCTURE_TASKS`** in `tests/test_scheduled_features_have_settings.py` with a written rationale (used for tasks that should always run, like `_task_resolve_predictions`).
5. **Update test stubs** in `tests/test_today_integration.py` so the integration test stubs out the new task (otherwise the test times out hitting your task's external dependencies).

### 4b. Guardrails that fire

- `test_scheduled_features_have_settings` — fails if the task runs unconditionally and isn't on `INFRASTRUCTURE_TASKS`.
- `test_today_integration` — fails if scheduler wiring regresses.

## 5. Adding a new specialist to the ensemble

The 5-specialist ensemble is one of the highest-leverage decisions in the system. A new specialist is a non-trivial change.

### 5a. Steps

1. **Decide what role the specialist plays.** Existing specialists: earnings_analyst, pattern_recognizer, sentiment_narrative, risk_assessor, adversarial_reviewer. A new specialist must have a clearly differentiated reasoning surface, otherwise its verdict will correlate with an existing one and add cost without value.
2. **Add the specialist class** to `ensemble.py`. Follow existing specialist class patterns: subclass with system prompt + feature subset.
3. **Define veto authority.** Default: not authorized. If the specialist should be able to veto trades regardless of the others (like `risk_assessor` and `adversarial_reviewer`), add it to the `VETO_AUTHORIZED` set.
4. **Update the synthesizer** in `ensemble.run_ensemble` to handle the new verdict slot.
5. **Add to `disabled_specialists` allowlist** so the auto-disable mechanism can skip it under bad calibration.
6. **Update the Platt-scaling layer** in `specialist_calibration.py`.
7. **Update health check** in `_task_specialist_health_check` if the specialist needs special handling.
8. **Render verdicts** in the AI Awareness ensemble panel (`templates/ai.html` Awareness tab).
9. **Add tests:** the specialist's cost ledger entry should be tracked; backfill from existing predictions should produce reasonable initial calibrators.

### 5b. Cost considerations

Every additional specialist multiplies the per-cycle AI cost. The platform's design budget is ~$0.014 per cycle for all 5 specialists + 1 batch call. Adding a 6th specialist should be a deliberate choice; budget for it.

## 6. Adding a new option strategy

Multi-leg option strategies are constructed via `options_multileg.py` builders.

### 6a. Steps

1. **Add a builder function** in `options_multileg.py`:
   ```python
   def <strategy_name>(symbol, expiry, *strikes, qty=1, spot_price=None) -> OptionStrategy:
       """Docstring."""
       legs = [
           OptionLeg(...),  # for each leg
       ]
       return OptionStrategy(name="<strategy_name>", legs=legs)
   ```
2. **Update `execute_multileg_strategy`** if the strategy needs special validation (e.g. credit vs debit handling).
3. **Update `evaluate_for_roll`** in `options_roll_manager.py` if the strategy is creditable and should be roll-managed.
4. **Add the strategy name** to the AI prompt's allowed-strategies list in `ai_analyst._build_batch_prompt`.
5. **Update `options_strategy_advisor.py`** if the strategy is a recommendation surface (e.g. a new income strategy alongside covered_call).
6. **Add tests:** unit test the builder; integration test via `options_backtester.py`.

## 7. Adding a new self-tuning rule (Layer 1)

A tuning rule is a function that buckets resolved predictions and adjusts a parameter based on per-bucket performance.

### 7a. Steps

1. **Define the rule** in `self_tuning.py` as `_optimize_<param_name>(db_path, ctx) -> Optional[Dict]`. Follow existing rule patterns.
2. **The rule must:** read `ai_predictions` rows, bucket them by parameter value range, compute differential win rate, and return either `None` (no change) or `{old_value, new_value, reason}`.
3. **Apply the change** by calling `update_trading_profile(profile_id, <col>=new_value)`.
4. **Register the rule** in the dispatcher list inside `run_self_tune` so it fires on the daily cycle.
5. **Bound the change.** Each tuning rule's step size should be ≤ 20% of the parameter's range to prevent oscillation.
6. **Add tests** that verify: rule returns None when insufficient data; rule moves toward winning bucket; rule respects bounds.

### 7b. Guardrail

- `test_every_lever_is_tuned` — verifies the parameter is now considered "tuned" by the rule.

## 8. Adding a new specialty data source (e.g. a 5th alt-data scraper)

The platform already ships with 4 alt-data scrapers in `/opt/quantopsai-altdata/`. Adding a 5th follows the same pattern.

### 8a. Steps

1. **Build the scraper** as a standalone Python project under `/opt/quantopsai-altdata/<name>/`. It should:
   - Have its own venv and dependencies (don't pollute QuantOpsAI's venv).
   - Maintain a SQLite database `<name>.db`.
   - Run on a daily cron (02:00 ET typical).
   - Log to `/opt/quantopsai-altdata/logs/`.
2. **Add a read helper** in `alternative_data.py` that queries the SQLite at decision time. Use `_altdata_query` for consistency.
3. **Wire to the aggregator** + meta-model + signal weights + AI prompt as in §2.
4. **Update `ALTDATA_BASE_PATH`** documentation if the layout changes.

## 9. Adding a new dashboard panel

A panel is a section on the AI dashboard's tabs (Brain / Strategy / Awareness / Operations).

### 9a. Steps

1. **Decide which tab.** Brain = "what the AI's brain looks like"; Strategy = "what's working"; Awareness = "what the AI sees right now"; Operations = "how the system is tuning itself."
2. **Add an HTML block** in `templates/ai.html` in the appropriate tab's section. Follow existing panel patterns for styling.
3. **Add an API endpoint** in `views.py` for the panel's data:
   ```python
   @views_bp.route("/api/<panel-data>/<int:profile_id>")
   @login_required
   def api_<panel>(profile_id):
       profile = get_trading_profile(profile_id)
       if not profile or profile["user_id"] != current_user.effective_user_id:
           return jsonify({"error": "Profile not found"}), 404
       # ... fetch + return JSON ...
   ```
4. **Add JS loader** in the same `templates/ai.html` file. Follow existing loaders (e.g. `loadAttentionSignals`) as templates.
5. **Add display names** for any new identifiers in `display_names._DISPLAY_NAMES`.

### 9b. Guardrails

- `test_no_snake_case_in_user_facing_ids` — fails if you render raw snake_case.
- `test_no_guessing` — fails if your JS references API field names that don't match the actual response.

## 10. Adding a new validation gate

A validation gate is a hard `if`-block that blocks AI-proposed trades.

### 10a. Steps

1. **Add the gate** in `_validate_ai_trades` in `trade_pipeline.py`. Follow existing gates as templates.
2. **Log the rejection reason** so it surfaces on the AI Awareness vetoed-trades panel.
3. **Add a per-profile toggle** if the gate is opt-in (per §3 above).
4. **Document in `docs/08_RISK_CONTROLS.md` §4.**
5. **Add tests:** verify the gate fires under the expected conditions; verify it does NOT fire when conditions are clear.

## 11. Modifying the AI prompt

The AI prompt is assembled in `ai_analyst._build_batch_prompt`. Modifications should be careful — the prompt structure affects every decision the system makes.

### 11a. Steps

1. **Decide which section** the new content belongs in: candidate-level, portfolio-level, market-context, learned patterns.
2. **Add the rendering logic** in the appropriate `_build_*_section` function.
3. **Wire upstream data** through `_build_market_context` or `_build_candidates_data` as appropriate.
4. **Verbosity-aware:** wrap rendering in `_verbosity(<section>) == "brief"` etc. checks if the section can be brief / normal / detailed.
5. **Update `display_names`** for any new identifiers.

### 11b. Cost impact

Prompt changes affect token count, which affects cost. Larger prompt = higher cost per cycle. The cost guard catches budget breaches but doesn't preempt prompt growth. Profile cost impact before merging.

## 12. Modifying the schema (large refactor)

If a change touches multiple tables (column rename, table split, etc.), follow this stricter process:

1. **Write a migration script** in `migrate.py` (or a new `migrate_<change>.py`) that handles the transformation idempotently.
2. **Test on a backup of prod data** before deploying.
3. **Deploy in two phases:** first deploy the migration; verify; then deploy the dependent code.
4. **Update CHANGELOG** with explicit migration notes.

## 13. Common pitfalls

- **Forgetting to add a `MANUAL_PARAMETERS` entry.** The lever guardrail catches this, but the error message is generic. Be explicit with the rationale.
- **Adding a `getattr(ctx, "x", default)` without a UserContext field.** Silent default. The `test_ctx_field_round_trip` guardrail catches this.
- **Caching a fetcher that should NOT be cached** (e.g. a real-time price). Read existing helpers carefully.
- **Adding a feature to the meta-model without rendering it** anywhere. The UI-coverage guardrail catches this.
- **Blocking on an external API in a per-cycle path.** Per-cycle code must be sub-second. Fetch heavy data via daily scheduler tasks and read from cache.

## 14. CHANGELOG discipline

Every behavior-changing commit must include a CHANGELOG entry. The format:

```markdown
## YYYY-MM-DD — <short title> (Severity: <critical|high|medium|low>, <type>)

<Description of what changed and why.>

**What ships:**
- <items>

**Tests:** <test files added>.

**Honest limits:** <if any>.
```

This is enforced by `test_recent_py_commits_paired_with_changelog`.

## See also

- `docs/04_TECHNICAL_REFERENCE.md` — module map.
- `docs/05_DATA_DICTIONARY.md` — schema reference.
- `docs/10_METHODOLOGY.md` — engineering principles.
