# 10 — Methodology

**Audience:** anyone reviewing or extending the system; reviewers asking "is this rigorous or improvised?"
**Purpose:** capture HOW decisions get made on this codebase, so that future contributors (including the operator's future self and any AI assistants) can extend the system without violating its spirit.
**Last updated:** 2026-05-03.

## What this document is

QuantOpsAI is opinionated software. The opinions show up in conventions that are not obvious from reading individual modules but that, taken together, define the engineering and research culture of the project. This document captures those opinions explicitly so they can be checked against and propagated.

## 1. Principles

### 1.1 No half measures

When a feature ships, it ships fully wired. A "quick fix" or "MVP that we'll iterate on" is rejected at code review. The system does not accept shipping an item that has any of:

- A schema column without an entry in the `update_trading_profile` allowlist (the column couldn't be saved through the UI even if a form submitted it).
- A user-tunable parameter without a settings UI control.
- A meta-model feature without a place a human can see its current value.
- A scheduled per-profile task without an enable/disable toggle, OR membership on the explicit `INFRASTRUCTURE_TASKS` allowlist with a written rationale.
- A new module without tests.
- Tests that skip on import failure or "pre-existing data" — every skip is treated as a hidden failure.

Each of these is enforced by a guardrail test. The guardrails are listed in §3 below.

### 1.2 Honest limits

Every approximation, simplification, or known limitation is marked with the literal token `**Honest limits:**` (or `# Honest limits:` in code) at the call site or module docstring. Examples:

- `slippage_model.py`: "K is currently fitted from paper fills; real fills will deviate."
- `mc_backtest.py`: "MC samples slippage IID per trade; correlated regimes aren't captured."
- `risk_stress_scenarios.py`: "Older scenarios (1987, dot-com) only have French factors."

These are not aspirational doc strings — they are surfaced in `OPEN_ITEMS.md` §11 and informed the proposed `next batch` work order.

### 1.3 No hidden levers

Every parameter that influences trade decisions must be discoverable:

- It appears in the settings UI, OR
- It's on the `MANUAL_PARAMETERS` allowlist with a written rationale (e.g., "Strategic AI choice — opt-in via `ai_model_auto_tune`"), OR
- It's auto-tuned (regression test confirms `update_trading_profile(profile_id, <col>=value)` is called from `self_tuning.py`).

A schema column failing all three conditions fails the `test_every_lever_is_tuned` guardrail.

The same principle extends to:

- Meta-model features (`test_meta_features_have_ui`) — every numeric / categorical feature must surface in templates, views, or the AI prompt.
- Weightable signals (same test) — every entry in `signal_weights.WEIGHTABLE_SIGNALS` must be visible.
- Scheduled tasks (`test_scheduled_features_have_settings`) — every per-profile task either has a toggle or is allowlisted as infrastructure.

### 1.4 Every decision is journaled

Every AI prediction, every strategy vote, every specialist verdict, every parameter change made by the self-tuner, every option position, every risk halt — all journaled to per-profile SQLite databases. Three reasons:

1. **The journal is the proprietary asset.** Resolved AI predictions in the system's own feature distribution cannot be replicated by competitors.
2. **It's the single source of truth.** Anything derived (Kelly recommendations, calibrators, learned patterns) traces back to journal rows.
3. **It's an audit trail.** When a trade goes wrong, you can reconstruct exactly what the AI saw, what the specialists said, and what the meta-model thought.

### 1.5 Forward-only, not retroactive

When the system adds a new feature (signal, gate, risk layer), it ships forward-only. The system never goes back and re-labels historical decisions to "what they would have been with the new feature." This is enforced for two reasons:

1. **Honesty.** Retroactive re-labeling would inflate apparent system performance.
2. **Reproducibility.** A backtest that doesn't deterministically reproduce when re-run is suspect.

The acknowledged consequence: the meta-model trains on rows where new features are zero / unset. This is a small bias in a large feature set; it is preferred over the alternative.

### 1.6 Deferring is a first-class action

When work is pulled out of scope, it is enumerated in `OPEN_ITEMS.md` with a status (`⏳ OPEN free`, `💰 OPEN paid`, `🔒 DEFERRED`) and a written rationale. There is no oral deferral. Anything not in `OPEN_ITEMS.md` doesn't exist.

The quarterly grep sweep (described in `OPEN_ITEMS.md` §"How this list is maintained") catches new code-level deferrals that snuck in between major doc passes.

### 1.7 Test discipline

- 100% pass rate. Zero skipped. Skipping is treated as failure.
- Tests that depend on external state (live API, real DB) are mocked with explicit fixture setup; no network calls in the suite.
- Every commit touches the test suite if it touches behavior. The pre-existing CHANGELOG-pairing test enforces that production `.py` commits ship with a CHANGELOG entry.
- Random ordering enabled (`pytest-randomly`) catches order-dependent test pollution.

### 1.8 The AI is the apex policy, not a feature

A subtle but important architectural choice: the AI is not "an enhancement" to a rules-based system. The rules-based components (strategy votes, ranking, specialist ensemble, gates) are scaffolding for the AI's decision. Downstream, only objective gates block the AI's decision; nothing second-guesses the AI's discretion within the gate envelope.

This means:

- The AI sees the FULL picture (otherwise it's making decisions on incomplete information).
- The AI gets ALL relevant context (otherwise we'd be filtering out useful signal).
- The AI's reasoning is captured (so we can audit and improve).
- The system's job is to provide BETTER context over time — not to replace the AI's judgment.

## 2. Anti-patterns the system actively rejects

### 2.1 "Quick fix" without test coverage

A bug fix that doesn't ship with a regression test is rejected. The CHANGELOG entry must name the test or include a TODO to add one. This is enforced by code review and reinforced by the no-pre-existing-failures convention (every test failure must be fixed at the time it surfaces; deferring it requires a written explanation in CHANGELOG).

### 2.2 Hidden levers, hidden features, hidden state

The platform's recurring failure mode (acknowledged on multiple occasions) is shipping new infrastructure that the user can't see, configure, or disable. The four guardrails in §3 are the response to that pattern. Any future feature that triggers a guardrail violation requires either fixing the wiring or explicitly extending the allowlist with a rationale.

### 2.3 "Pre-existing failure" as an excuse

Every test failure is investigated in the session it surfaces. The fact that a failure existed in the previous session is not exculpatory — it just means the previous session left it. This convention is documented in the user's auto-memory (feedback memory: don't dismiss failing tests).

### 2.4 Snake_case and code identifiers in user-facing copy

The user does not see `meta_pregate_threshold`, `enable_intraday_risk_halt`, or `2008_lehman` rendered as raw strings. Every user-facing surface routes through `display_names.display_name` or an equivalent server-side label resolver. This is enforced by `test_no_snake_case_in_user_facing_ids` and `test_no_snake_case_in_api_responses`.

### 2.5 Time-based excuses

The system runs 24/7 on a cloud server. Phrases like "tonight," "tomorrow morning," "let it bake overnight," or "wrap here for today" do not appear in code review or session notes. The system either operates correctly continuously or it has a bug.

## 3. Guardrail tests

Every test in this list either prevents a class of regression OR detects drift in an architectural invariant. They run on every commit.

| Guardrail test | Prevents |
|---|---|
| `test_every_lever_is_tuned` | Schema columns that aren't either auto-tuned or explicitly enumerated as user-set. |
| `test_meta_features_have_ui` | Meta-model features (NUMERIC, CATEGORICAL, weightable signals) without UI surfaces. |
| `test_scheduled_features_have_settings` | Scheduled per-profile tasks without enable/disable toggles, OR allowlisted INFRASTRUCTURE without rationale. |
| `test_no_snake_case_in_user_facing_ids` | Identifiers (sectors, factors, scenarios, parameters) rendering as raw `snake_case` in HTML. |
| `test_no_snake_case_in_api_responses` | API responses leaking `PARAM_BOUNDS` keys to the JS layer. |
| `test_no_guessing` | Hardcoded data assumptions in templates / JS that don't match the actual API field names. |
| `test_today_integration` | Scheduler wiring regression — DB backup, AI cost ledger, and 25+ task invocations. |
| `test_recent_py_commits_paired_with_changelog` | Production `.py` commits without CHANGELOG entries. |

## 4. Conventions for adding new things

The integration guide (`docs/11_INTEGRATION_GUIDE.md`) is the procedural reference. The methodological invariants are:

### 4.1 Adding a new strategy

- Lives in `strategies/<strategy_name>.py`.
- Pure function `run(symbol, market_type, df, params) → dict`.
- Has a baseline backtest that passes the rigorous gauntlet's 10 gates.
- Auto-deprecates on alpha decay (handled by `alpha_decay.py`; no per-strategy code needed).
- If the strategy's vote should be tunable per-profile (Layer 2), it gets an entry in `signal_weights.WEIGHTABLE_SIGNALS`.

### 4.2 Adding a new signal / feature

- If numeric: add to `meta_model.NUMERIC_FEATURES`. The UI-coverage guardrail will fail until the feature is referenced in templates / views / AI prompt.
- If categorical: add to `meta_model.CATEGORICAL_FEATURES` with the value list. Same UI-coverage requirement.
- If user-controllable: add to the Layer 2 weight tuner via `signal_weights.WEIGHTABLE_SIGNALS`.
- Add a fetcher / collector in `alternative_data.py` (or its own module if substantial).
- Plumb through `_build_features_payload` in `trade_pipeline.py` so the meta-model trains on it.
- Render in the AI prompt under ALT DATA in `ai_analyst._build_alt_data_section`.

### 4.3 Adding a new schema column

- Add to the migration list in `models.py`.
- Add to `update_trading_profile`'s `allowed_cols` set.
- Add to the form parser in `views.py:save_profile` (if user-editable).
- Add a control to `templates/settings.html`.
- Add to `UserContext` and to `build_user_context_from_profile`.
- Either:
  - Add a tuning rule in `self_tuning.py` (auto-tuned), OR
  - Add an entry to `MANUAL_PARAMETERS` in `test_every_lever_is_tuned.py` with a written rationale.

The guardrail will fail until all six locations are updated.

### 4.4 Adding a new scheduled task

- Lives in `multi_scheduler.py` as `_task_<name>(ctx)`.
- Wrapped in `run_task(...)` with a `seg_label` for logging.
- Wrapped in `if getattr(ctx, "enable_<name>", default):` check, AND the column exists in `trading_profiles` schema with a settings.html control, OR
- Added to `INFRASTRUCTURE_TASKS` allowlist with a written rationale.

The guardrail will fail until the task is gated or allowlisted.

### 4.5 Adding a new specialist to the ensemble

- Add the specialist class to `ensemble.py`.
- Plumb through `run_ensemble` synthesizer (verdict aggregation).
- Add to `disabled_specialists` allowlist (so health check can disable it).
- Add Platt-scaling layer in `specialist_calibration.py`.
- Update `_task_specialist_health_check` if the specialist needs special calibration handling.
- Render verdicts in the AI Awareness ensemble panel (`templates/ai.html` Awareness tab).

## 5. Resolving disagreements

When evidence and conviction conflict:

- **Evidence wins.** A backtest gauntlet failure is dispositive — the strategy doesn't ship live, even if the operator believes it will work.
- **Honest limits beat optimistic claims.** When a model has known bias, it's surfaced in `OPEN_ITEMS.md` §11 and in the relevant docstring, not buried.
- **Tests beat assertions.** A claim that "this is structurally impossible" must be backed by a guardrail test. Otherwise the claim is hopeful.

When user feedback and inferred convention conflict:

- **The user wins.** Auto-memory captures user-stated preferences (no time excuses, full dev loop, alpaca-first data, etc.) and they override anything an AI assistant would otherwise default to.

## 6. The auto-memory layer

`/Users/mackr0/.claude/projects/-Users-mackr0-Quantops/memory/MEMORY.md` is loaded into context for every session. It tracks:

- Operator profile and preferences.
- Project state (load-bearing: virtual-account architecture, Alpaca-first data rule, capital allocation policy).
- Feedback (corrections + confirmations from prior sessions).
- References (where to find external systems).

This is not the same as the `OPEN_ITEMS.md` and CHANGELOG layer. Auto-memory is for **inter-session continuity** of conventions and preferences. The doc layer is for **codified architectural knowledge.** Both exist; both are maintained.

## 7. Engineering quality is a feature

The system is opinionated about engineering quality because in a system where the proprietary asset is a corpus of resolved AI predictions, every untracked drift is a permanent loss of information. Concretely:

- A meta-model feature added without UI surface is unusable (operator can't audit it).
- A scheduled task without a toggle creates phantom load (cost or latency that's hard to attribute).
- A test that silently skips creates the illusion of safety where none exists.
- A schema column without an `update_trading_profile` allowlist entry breaks the form save path silently.

Each of these failure modes was observed in this project's history. The guardrails are the encoded response to specific incidents.

## 8. What the system explicitly does not optimize for

- **Throughput / latency.** This is a 5-15 minute cycle system. Sub-second execution is out of scope.
- **Multi-tenant deployment.** Single operator, single droplet, single Alpaca relationship.
- **Open-source distribution.** Personal project; no license for redistribution.
- **Frontend UX polish.** The web UI is functional, not designed. Operators are the audience.

## See also

- `docs/01_EXECUTIVE_SUMMARY.md` — what this is, why it might be valuable.
- `docs/02_AI_SYSTEM.md` — the AI / ML technical detail.
- `docs/04_TECHNICAL_REFERENCE.md` — system architecture.
- `docs/11_INTEGRATION_GUIDE.md` — extending the system.
- `OPEN_ITEMS.md` — single source of pending work.
