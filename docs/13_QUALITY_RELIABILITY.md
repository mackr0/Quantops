# 13 — Quality, Safety, and Reliability

**Audience:** the operator, anyone reviewing whether the system is trustworthy enough to put money on, and any future AI assistant editing this codebase.
**Purpose:** the current strategy for keeping the platform safe and reliable — test discipline, AI-behavior guardrails, in-flight production controls, backup and recovery. This document describes what is in place today; the history of changes lives in `CHANGELOG.md`.

---

## 0. Why this document exists

QuantOpsAI ships changes daily, often via AI-assisted edits. The threat model is not just "does the code compile" — it includes "did the assistant hallucinate a column name?", "did the assistant remove a guard while fixing something else?", "did the assistant claim a fix is done when it isn't?". The safety system has three layers, all enforced automatically:

1. **Pre-commit / CI tests** (413 files, 5,133 tests, 3 environment-dependent skips — an `_EMPTY_FIRE_EXEMPT` rule whose purpose IS to fire on minimal context, a segment-context builder that needs a production-like env, and the strategy-zombie check which needs accumulated prediction data) — must pass before merge.
2. **Production-side controls** — defense-in-depth gates that catch what slips past tests.
3. **Backups + rehearsed disaster recovery** — assume something will eventually break.

Each layer is documented below. The AI-behavior guardrails (§3) are the answer to "how do I stop paying for the same class of bug being re-introduced?".

---

## 1. Test discipline (the baseline)

| Property | Value |
|---|---|
| Test runner | `venv/bin/python -m pytest tests/ -q` |
| Tests skipped | **0** — skipping is treated as failure |
| Per-test timeout | 30 seconds (60 for slow integration tests) |
| Order independence | `pytest-randomly` flips order every run |
| External APIs | All mocked — no network calls in the suite |
| Pre-commit hook | full suite must pass before `git commit` succeeds |

**Hard rules** (enforced by guardrails — §3):
- Every production `.py` commit must include a CHANGELOG entry.
- Every test failure is investigated in the session it surfaces; "pre-existing failure" is not exculpatory.
- Mocks must represent the **bad / failing** state, not just the eventual happy path.

---

## 2. The test taxonomy

Tests fall into four categories. The first three are unit / integration tests; the fourth — **guardrail tests** — is what makes this project distinctive.

### 2.1 Per-module unit tests
One per major production module. Cover the function's contract: happy path, edge cases, error path. Stock test pyramid.

### 2.2 Integration tests
- `test_today_integration.py` — scheduler wiring; stubs every `_task_*` and asserts the dispatch order, DB-backup invocation, AI cost ledger calls, etc. Catches scheduler-plumbing regressions.
- `test_pipeline.py` — end-to-end cycle through `trade_pipeline.run_trade_cycle` with mocked Alpaca / AI.
- `test_web.py` — Flask app starts cleanly and serves the major routes.

### 2.3 Regression tests for specific incidents
Each historical incident in CHANGELOG names the test that prevents it from recurring. Examples:
- `test_no_undefined_logger.py` — bare `logger.X(...)` references without a `logger=` definition.
- `test_pending_fill_state_machine.py` — close-path state transitions tied to broker confirmation.
- `test_db_integrity.py` — DB corruption detection + restore-from-backup behavior.

### 2.4 Guardrail tests (the AI-behavior layer)
Static / cross-cutting checks that enforce architectural invariants. These prevent regression of an entire **class** of bug, not just one incident. Detailed in §3.

### 2.5 Offline snapshot-replay harness (`snapshot_audit.py`)
Hand-authored fixtures encode the author's mental model, so they pass even when that model is wrong — which is exactly how the phantom-equity incident shipped green. The snapshot harness runs ground-truth invariants against **real** per-profile DB snapshots (prod backups), no live broker required:

- **Order-id truth** (per profile, stocks) — `get_virtual_positions` net per symbol == the signed sum of that profile's own confirmed fills.
- **Order-id ownership** (cross profile) — the load-bearing isolation invariant: every fill-bearing row carries an alpaca order_id, and no order_id (entry or protective) appears in more than one profile's book. This makes profile-bleed and orphans decidable from the order_id alone. (Run against the whole 06-17/06-18 corrupt cohort — 234 backups — it confirmed **zero bleed and zero untracked fills**: the incident was a single-profile reconstruction bug, never bleed.)
- **Filled-but-unpriced** — a `closed` (filled) row with no price is a real move the position view drops (escalated); a non-`closed` unpriced row is a softer data-quality note.
- **Decomposition** — (equity − capital) == realized + unrealized; reported as SKIPPED, never silently half-run, when capital is unavailable.

It is backed by three test layers (`tests/test_snapshot_audit_*`, `test_position_property_*`, `test_pipeline_replay_*`), including a property test whose **completeness** half asserts the harness fires on injected corruption (orphan closed-buy, closed-unpriced) rather than staying silent. **Honest scope:** this is a journal-internal + ownership net; it does **not** reconcile against the live broker — that stays the job of `certify_books` (the per-cycle gate, §4.2) and `aggregate_audit`. The harness itself was adversarially reviewed and nine false-greens fixed before it was trusted.

---

## 3. AI-behavior guardrails — the "stop the assistant from doing the same stupid thing twice" tests

This is the set of tests that exists specifically to constrain AI-assisted edits. Each one prevents a class of failure that an assistant has historically been observed to introduce.

### 3.1 No hallucinated names

| Guard | Catches |
|---|---|
| `test_every_lever_is_tuned` | Schema columns that aren't either auto-tuned by `self_tuning.py` OR explicitly enumerated in `MANUAL_PARAMETERS`. Prevents the assistant from adding a column nobody can save through the UI. |
| `test_meta_features_have_ui` | Meta-model `NUMERIC_FEATURES` / `CATEGORICAL_FEATURES` / weightable signals without a UI surface. Prevents extending the model with features the operator can't audit. |
| `test_scheduled_features_have_settings` | New scheduled per-profile tasks without an enable/disable toggle, OR allowlisted on `INFRASTRUCTURE_TASKS` without a rationale. |
| `test_no_guessing` | Hardcoded API field names in JS / templates that don't match the actual API response. Prevents the assistant from guessing a field name (`prediction.timestamp` vs `prediction.created_at`). |

The user's standing rule: *"Never guess table names, column names, or function signatures. Always read the schema or source code first."* These tests turn that into an enforced contract.

### 3.2 No silent failures

| Guard | Catches |
|---|---|
| `test_no_silent_except_pass` (strict, baseline empty) | AST scan over **all** production source. Any `except: pass` or `except Exception: pass` fails unless annotated with `# SILENT_OK: <rationale>` immediately above the `except` keyword. Replaces the per-module check below — covers the entire codebase, not just trade-execution paths. |
| `test_json_decode_paths_safe` (strict, baseline empty) | AST scan for `json.loads()` / `json.load()` calls without a `try` ancestor. Fails unless wrapped in try/except or annotated with `# JSON_OK: <rationale>`. Catches malformed-cache and malformed-API-response crash classes. |
| `test_every_db_connection_is_closed` (strict, baseline empty) | AST scan for `sqlite3.connect()` without context-manager / try-finally close. The 2026-05-14 audit converted all 93 historical direct-leak sites to safe patterns; baseline empty so any new leak fails on first introduction. |
| `test_factory_helper_callers_have_try_finally` (strict, no baseline) | Class-level AST scan for `conn = factory(...)` assignments where `factory` is one of `_get_conn`, `_open_journal_conn`, `open_profile_db`, `_open_conn`. Each must be inside `with closing(...)` or wrapped in try/finally. Closes the gap that the original factory-pattern detector left open: a caller of a connection factory without try/finally still leaks on exception. The 2026-05-14 audit fixed 131 such sites including 3 ACTUAL leaks where conn was never closed at all. |
| `test_broker_submit_invariants::test_no_bare_except_pass_on_db_or_broker_calls` | Tightened version of the above for trade-execution modules. Kept as belt-and-suspenders. |
| `test_silent_failure_fixes_*` | Per-site static checks that historical WARNING-log markers stay in place. Catches refactor-strips-warning regressions. |
| `test_no_undefined_logger` | Files using `logger.X(...)` without `logger = logging.getLogger(__name__)` defined. Catches the latent NameError class on conditional code paths. |

The user's standing rule: *"Every except: pass is a potential silent failure — log at WARNING minimum."*

**2026-05-14 audit completed:** All 260 historical `except: pass` sites and all 3 historical unsafe `json.loads` sites were classified and either annotated (`# SILENT_OK:` for intentional best-effort patterns: cache writes, per-loop continues, AI-prompt enrichment fallbacks) or fixed (1 silent risk-halt-gate bypass surfaced via `logger.warning`; 3 corrupt-JSON crashes wrapped with sensible fallbacks). Both ratchets now strict (baseline `{}`) — any new silent swallow or unguarded `json.loads` fails CI. See CHANGELOG 2026-05-14 for the per-fix detail.

### 3.3 No `snake_case` leaks to the user

**The architecture.** Snake_case leakage cannot be caught at the source — the AI generates arbitrary identifiers in narrative text, and any future code path that emits a new label is a new failure surface. The contract is therefore enforced at the render boundary: every dynamic-content render passes through a mandatory sanitization filter (`display_names.humanize`), and a structural test asserts that every dynamic-content interpolation in the template tree uses the filter. The filter is idempotent (safe to apply twice) and falls back to Title-Casing for any unknown identifier, so a new label the LLM invents tomorrow renders readably without any code change.

**The contract:** every dynamic-content render goes through `display_names.humanize` — the `| humanize` Jinja filter in templates, or the `humanize(...)` function call in `views.py`. The filter:

- Replaces known identifiers from `_DISPLAY_NAMES` with their canonical labels.
- For UNKNOWN snake_case / UPPER_SNAKE_CASE patterns, falls back to Title-Casing (e.g. `STRONG_BUY` → "Strong Buy", `quantum_thresher_signal` → "Quantum Thresher Signal").
- Is idempotent — running it twice gives the same result, so defensive double-application is safe.
- Handles both lowercase and UPPER snake, including embedded digits (`roc_10`, `momentum_20d_gain`).

**Adding a new identifier to `display_names._DISPLAY_NAMES` is now optional.** It's only needed when you want a specific canonical label that differs from the default Title-Case (e.g. "Insider Buying Cluster" instead of "Insider Cluster"). The filter handles unknowns automatically — a future identifier the LLM invents tomorrow renders readably without any code change.

| Guard | Catches |
|---|---|
| `test_no_snake_case_in_rendered_output` | The single test that enforces the render-boundary contract. Three layers: (1) filter behavioral pin — every known leak shape plus a synthetic "future" identifier; (2) static template audit — every `{{ ... }}` interpolation of a dynamic-content field (`ai_reasoning`, `reasoning`, `reason`, `narrative`, `summary`, `description`, `detail`, `message`, `title`) MUST pipe through `humanize` or `display_name`; (3) end-to-end render simulation — renders the trades-table macro and the activity-feed handler with synthetic LLM-leaky data and asserts no raw tokens survive. Includes an inverse self-test that confirms the test catches a regression if the filter is removed. |
| `test_signal_humanization_structural` | AST-walks `strategies.py` and `signal_weights.py` to discover every signal type the strategy layer can emit, plus enumerated AI-emitted signals (STRONG_BUY, MULTILEG_OPEN, etc.). Asserts each humanizes to a clean form (no underscores, no run of all-caps tokens). Catches the case where the filter ITSELF starts producing ugly output for a new signal type — distinct from the rendered-output test because it checks the source set rather than waiting for the signal to appear in a render path. |
| `test_display_names` | Mapping-dict integrity. Pins canonical labels for known identifiers (Mack-approved spellings like "Bond/Stock Divergence" with the slash, "Sentiment & Narrative" with the ampersand). |

**The fix when a leak surfaces** is always one of:
1. Apply `| humanize` at the template render site.
2. Apply `humanize(...)` to the field server-side in `views.py` before `jsonify`.

The fix is **never** to add an entry to `_DISPLAY_NAMES` for the specific token, never to widen an allowlist, never to add a per-string `.replace()`, and never to "fix the AI" by tuning the prompt to avoid emitting snake_case. The display layer is the contract; the AI can keep emitting whatever it emits.

The repeated user complaint: *"never ship raw snake_case in the UI."* The architecture makes that the default-safe posture.

### 3.4 No unrealistic mocks

| Guard | Catches |
|---|---|
| `test_broker_submit_invariants::test_filled_avg_price_mocks_include_none_case` | Test files that mock `api.get_order(...).filled_avg_price` returning a numeric value as the immediate-after-submit reply MUST also exercise the `None` case. Real Alpaca paper takes 50–500 ms to fill; mocks that always return a numeric price hide bugs that are real in production. |

The lesson: a passing test is worthless if the mock doesn't represent the failing state production sees.

### 3.5 No runaway broker submissions

| Guard | Catches |
|---|---|
| `test_broker_submit_invariants::test_every_option_submit_passes_position_intent` | Every `api.submit_order(...)` site in `options_trader.py` and `options_multileg.py` must include `position_intent` (`buy_to_open` / `sell_to_open` / `buy_to_close` / `sell_to_close`). Without it, Alpaca async-cancels short option opens. The check recognizes the `**kwargs` pattern via `_alpaca_leg_dict` in the preamble. |
| `test_broker_submit_invariants::test_every_entry_executor_has_dup_guard` | `execute_multileg_strategy`, `execute_option_strategy`, `execute_pair_trade` must each contain a dup-guard marker. Without the guard, the AI re-proposing the same trade on consecutive cycles re-fires every cycle. |

### 3.6 No viewer-mutates-admin-state security holes

| Guard | Catches |
|---|---|
| `test_viewer_cannot_mutate_admin_state::test_every_mutating_endpoint_is_admin_required` | Static scan over every `@views_bp.route(..., methods=["POST"\|"PUT"\|"DELETE"\|"PATCH"])` decorator. The associated function MUST carry `@admin_required`. Endpoints intentionally writable by viewers go on `INTENTIONALLY_VIEWER_WRITABLE` (currently empty) with a written rationale. |
| `test_kill_switch_admin_only` | Specific endpoint case: viewer `activate` and `deactivate` requests must return 403; admin path still works; dashboard template gates the activate/deactivate buttons on `is_viewer`. |

### 3.7 No CHANGELOG-less commits

| Guard | Catches |
|---|---|
| `test_recent_py_commits_paired_with_changelog` | Production `.py` commits without a CHANGELOG entry on the same date. Encodes the standing rule: *every code change must include CHANGELOG.md and doc updates, no exceptions.* |

### 3.8 No feature ships without its UI / settings wiring

`test_every_lever_is_tuned`, `test_meta_features_have_ui`, `test_scheduled_features_have_settings`, `test_today_integration` together enforce that a new feature is fully wired before it can land. The "no half-measures" rule is encoded across these four guardrails.

---

## 4. Production safety controls (defense in depth)

These run at trade time, in addition to the pre-commit tests. Each is independent — any one is sufficient to stop the bleed.

### 4.1 Pre-trade gates (in priority order)
0. **Oversell door** — the single, unbypassable gate at the per-profile api factory (`user_context.get_alpaca_api` → `order_guard.GuardedAlpacaApi`). Every `submit_order` passes through it; a stock SELL that exceeds the profile's **own journal long** (`get_virtual_positions` on its own book — never the shared-account aggregate) is **refused before the broker sees it**, unless the order declares `intent="open_short"` (the genuine short-entry sites do). This is *prevention*, not detection: a re-armed naked sell — the 2026-06-18 phantom-equity vector — can't reach the broker. A guardrail test forbids any order module from building its own raw `tradeapi.REST` (which would be an unguarded door).
1. **Broker disconnect** — N consecutive Alpaca call failures → `BROKER_DISCONNECTED` until next success. Refuses new entries during an outage.
2. **Master kill switch** — manual or auto-flipped by the book-loss floor. Blocks every new entry across every profile until cleared. Admin-only toggle.
3. **Catastrophic single-trade gate** — proposed trade $ value > 5× profile recent average → refuse.
4. **Cross-profile concentration cap** — aggregate $ exposure to a single symbol > 25% of book → refuse.
5. **Drawdown pause** — per-profile drawdown threshold breach → refuse.
6. **Per-trade portfolio constraints** — sector cap, correlation cap, max positions, etc.

### 4.2 Per-cycle health checks
- **Stop-order coverage alarm** — if <80% of open longs have a broker protective stop, log + optional auto-kill.
- **Position runaway sentinel** — duplicate-submit / excessive single-trade qty detection.
- **AI consistency floor** — recent-100 directional win rate < 30% for 5 consecutive cycles → log error + optional auto-kill.
- **Crisis state monitor** — cross-asset distress signals; scales position sizes 1.0× → 0.25×.
- **Intraday risk halt** — drawdown / vol / sector / held-position-halt monitor; 60-min auto-clear.

### 4.3 Reconcile + audit (run at the trading-cycle cadence, ~5 min)
**Cadence (important):** detection AND correction run at the trading-cycle cadence, never slower — and never on a redundant blind poll. The integrity gate (§4.2), the aggregate **drift audit** (`aggregate_audit.audit_aggregate_drift`), and the **corrective reconcile** (`reconcile_with_ctx`) all run **once per orchestrator cycle** inside the scheduler — the audit and reconcile live in `_task_reconcile_trade_statuses` (audit first-active-profile gated). The per-cycle reconcile runs alongside actual trading activity (when there's genuinely new broker state to reconcile) and is the sole real-time clearing path (an NFLX settlement drift on 2026-06-22 self-cleared within a cycle, ~4 min). **There is no standalone reconcile cron** — the old `*/15` `reconcile_journal_to_broker.py` cron was removed 2026-06-22 as redundant duplicate polling: it re-hit the broker on a clock regardless of activity, doubling Alpaca load and risking rate-limiting for zero benefit (the scheduler already covers per-cycle reconcile). Alpaca grants no streaming entitlement (REST only), so the discipline is: poll at the cycle cadence, never poll the same state twice. If the scheduler stops, the **reconciler-heartbeat audit** (tier 7 below) alerts when reconcile hasn't run in 60 min.
- **`reconcile_journal_to_broker`** — compares every per-profile journal against Alpaca. Detects: phantom SELLs (logged but not filled), partial-sale drift, broker-side liquidation, canceled entries that the journal still claims as open. Auto-corrects by undoing phantoms and backfilling broker actions. Runs per-cycle in the scheduler (`reconcile_with_ctx`); the standalone cron was removed as redundant.
- **`aggregate_audit.audit_aggregate_drift`** — sums virtual positions across profiles routing to the same Alpaca account, compares to `api.list_positions()`. Catches multi-profile-overshoot scenarios where the sum exceeds broker actuals (logic bug).
- **`aggregate_audit.audit_account_value_parity`** — sibling check on the DOLLAR side: sum of virtual `market_value` per account vs sum of broker `market_value` per account. Catches divergence even when quantities match (missing options multiplier, stale marks, value-only logic bugs). Tolerance: `max($50, 0.1% of broker value)`. Drift surfaces on `/issues` as ERROR; no auto-reconcile because the correct fix depends on which side is wrong.
- **`aggregate_audit.audit_account_cash_parity`** — per Alpaca account, `broker_cash` should equal `sum(virtual_cash)` across profiles routing to it. Catches hidden broker cash flow (dividend, fee, manual deposit) and trades that hit broker but not journal. Tolerance: `max($50, 0.1% × broker_cash)`.
- **`aggregate_audit.audit_account_basis_parity`** — per `(account, symbol)` where both sides hold the position, broker `avg_entry_price` should match the qty-weighted virtual `avg_entry_price` across all profiles holding that symbol. Catches wrong-price fills, broken FIFO basis adjustment, multileg cost-allocation drift. Tolerance per-share: `max($0.05, 0.5% × broker_avg)`.
- **`integrity_audit.audit_equity_identity`** — per-profile self-consistency: `equity == initial_capital + Σ(realized) + Σ(unrealized)`. The journal's own algebra. Catches FIFO mismatches, hidden cash flows, market_value/unrealized_pl divergence — bugs that don't appear in broker comparisons because the broker is fine; the journal is internally inconsistent. Tolerance: $1.
- **`integrity_audit.audit_reconciler_heartbeat`** — verifies the per-profile reconciler ran in the last 60 minutes. Without this, silent cron failure would let all the other audits read stale state forever.
- **`audit_runner.detect_and_alert_new_drift`** — the signature-tracking email alerter (persists a signature per drift item to `quantopsai.db.audit_alerts`, alerts on first detection, logs resolution). **No longer scheduled (2026-06-18):** it ran *after* trading on a 10-min timer, only emailed, and defaulted to profile range 1-11 — so it never saw the experiment (145-154) and an email nobody acts on isn't a safety system. Live enforcement is now `multi_scheduler._run_integrity_gate()` (runs before entries every cycle, HALTS on a finding); the full seven-tier audit is surfaced on `/issues` (now scoped to the live profiles, not 1-11). `audit_runner` remains importable for ad-hoc use.

**Seven-tier integrity contract**:
1. Every trade carries a broker `order_id` (#157 perfect-matching invariant)
2. Quantities sum correctly per account (aggregate_audit)
3. Values sum correctly per account (account_value_parity, #165)
4. Cash sums correctly per account (account_cash_parity, #167a)
5. Per-share cost basis matches per (account, symbol) (account_basis_parity, #167b)
6. Each profile's own algebra balances (equity_identity, #166)
7. Reconciler ran within the last 60 minutes (reconciler_heartbeat, #170)

**Enforcement (2026-06-18 rework).** These checks are now run by `multi_scheduler._run_integrity_gate()` **before entries on every trading cycle** — not on a separate slower schedule. On any broker-drift or decomposition finding it **engages the kill switch**, so new entries halt on the same cycle the divergence appears (exits/covers still run); the operator is emailed on the first halt and the kill-switch reason shows on the dashboard. This replaced the old `audit_runner` cron, which ran *after* trading on a 10-min interval, only emailed, and was found to audit the wrong profile range (1-11) — so a ~$187K phantom-equity oversell across the experiment (profiles 145-154) went undetected for ~a day. Detection that only emails is not a safety system; the gate that halts is.

### 4.7 Per-bucket P&L auto-cutoffs

Visibility (the seven-tier contract) is the precondition for action, not action itself. The self-tuner is what converts visibility signals into automated parameter changes.

- **`_optimize_options_pnl_cutoff`** — reads 30-day realized P&L on rows where `occ_symbol IS NOT NULL`. If ≥10 closed options trades sum to less than `-3% × initial_capital`, flips `enable_options=0` on the profile to stop options bleed without operator intervention. Auto-re-enables after 14 days (per the bias-toward-trading principle — no permanent off-state); if the bleed resumes, the disable re-fires.

The pattern generalizes: every bucket with an `enable_X` flag can have a P&L-driven optimizer that watches its slice of `trades.pnl` and auto-flips the flag when the bucket consistently destroys value. Future expansions tracked as follow-ups in CHANGELOG.

### 4.4 The `pending_fill` state machine
SELL / COVER / option-close rows write `status='pending_fill'` on submit, NOT `closed`. `_task_update_fills` flips to `closed` once `filled_avg_price` arrives. Eliminates the phantom-close window where the journal would otherwise claim realized P&L the broker had async-canceled.

### 4.5 AI provider failover
N consecutive 5xx / timeout failures from the active AI provider trip a circuit; calls auto-route to the next configured provider (OpenAI → Google).

### 4.6 DB integrity
Every scheduler startup runs `PRAGMA quick_check` on every DB. Real corruption halts the scheduler with a deduplicated email alert. `restore_from_backup()` is one command — see §5.

---

## 5. Backups and disaster recovery

### 5.1 Daily backups
- `backup_daily.sh` runs from system cron at **05:00 UTC** every day.
- Uses sqlite3's online `.backup` (safe under concurrent writes).
- Snapshots: master DB + every per-profile DB + alt-data DBs to `/opt/quantopsai/backups/`.
- Filename format: `<dbname>.<YYYYMMDD-HHMM>`.
- **Retention: 14 days.** Older backups pruned automatically.
- Backup directory is excluded from `sync.sh`'s rsync so deploys don't delete snapshots.

### 5.2 Restore runbook
Single command: `restore_from_backup("<filename>")`.
1. Finds the latest backup, filtering out `-wal` / `-shm` sidecars and `corrupt-*` archive files.
2. Verifies the backup itself passes `quick_check` AND has a valid SQLite magic-bytes header.
3. Archives the corrupt original as `<filename>.corrupt-<TS>`.
4. Copies the verified backup into place.
5. Re-runs `check_db` on the restored file.

The full procedure has been rehearsed end-to-end on production with bit-identical recovery. Step-by-step is in `docs/07_OPERATIONS.md` §9.

### 5.3 What the system does NOT have (honest limits)
- No off-site backup replication. The 14-day local snapshots are the only copies.
- No formal RPO / RTO commitment. RPO is ~24h (last daily snapshot); RTO is minutes-to-hours (manual restore command).
- No multi-tenant isolation (single-operator design).

---

## 6. Operational verification scripts

Two complementary scripts live in the repo root:

### 6.1 `morning_health_check.sh` — daily operational check

Run every morning before market open. Drives off the seven-tier integrity contract + audit_runner. **Dynamically discovers active profiles** via `SELECT id FROM trading_profiles WHERE enabled = 1`, so it survives any profile rotation (cohort resets, profile additions / removals).

Sections:
- **§0 Services + deploy hygiene** — both services active, prod git = origin/main, gunicorn workers fresh.
- **§A Scheduler liveness** — last cycle completed within 20 min (reads `scheduler_status.json`); zero TASK FAILs since session start.
- **§B Integrity audits** — `audit_alerts` table exists; unresolved drift count grouped by audit type; every active drift item has been emailed (alert_sent flag).
- **§C Reconciler heartbeat** — per active profile, "Reconcile Trade Statuses" task ran within 60 min.
- **§D Activity capture** — `_task_capture_broker_activities` ran today on every profile; tallies DIV/OPEXP/OPASN captured in the 7-day window.
- **§E Daily equity snapshot** — every profile has a `daily_snapshots` row for today (after market close; tolerant pre-close).
- **§F Comparative-returns API** — `/api/comparative-returns` reachable and returns valid JSON.
- **§G Cost** — today's cumulative AI spend across all active profiles.
- **§H Alt-data freshness** — every alt-data DB refreshed within 30h.
- **§I Options bucket health** — per-profile 30-day options P&L; warns when any profile is below −3% of capital but the `_optimize_options_pnl_cutoff` (#171) hasn't fired yet.

### 6.2 `verify_first_cycle.sh` — post-deploy regression check

Legacy script kept for after-deploy verification of specific shipped fixes. Each check is pinned to a per-fix deploy cutoff so historic pre-fix failures don't false-alarm. Bump the cutoff when shipping a relevant fix.

Sections (hardcoded profile list — does NOT survive the fresh-experiment rotation; use this script ONLY against the legacy account topology):
- §0 Service health
- §A Core scheduler health (NameError, TASK FAILs)
- §B Cost & quality levers
- §C INTRADAY_STOPS_PLAN broker orders
- §D Long/short capability
- §E Trade-quality metrics
- §F Trade execution behavior
- §G Cost
- §H Alt-data scrapers
- §I PDUFA + AdComm
- §J UI guardrails

After the fresh-experiment cutover, `morning_health_check.sh` is the canonical daily check; `verify_first_cycle.sh` becomes opt-in for diagnosing specific regressions.

---

## 7. CHANGELOG and memory discipline

### 7.1 CHANGELOG
- Every commit that changes behavior gets an entry.
- Format: `## YYYY-MM-DD — short title (Severity: X, type)` with what broke, why it wasn't caught, what the fix does, why the new test would catch it next time.
- Enforced by `test_recent_py_commits_paired_with_changelog`.
- File: `CHANGELOG.md` at repo root — the project's institutional memory. **Date-stamped incident detail belongs here, not in this docs tree.**

### 7.2 Auto-memory (Claude assistant)
- File-based memory at `/Users/mackr0/.claude/projects/-Users-mackr0-Quantops/memory/`.
- Loaded into every Claude session.
- Captures user preferences, project state, feedback corrections, and references to external systems.
- Current rules saved as feedback memories:
  - Data integrity is paramount — no shortcuts ever.
  - No silent failures — every error must be surfaced and fixed.
  - Small verified deploys — push fixes only when safe, verify after deploy.
  - Zero tolerance for errors and lazy fixes.
  - Always commit after deploy.
  - Never guess names — read schema/source first.
  - Always update docs and changelog with every change.

These are the de-facto contract for what the AI is allowed to do on this codebase.

---

## 8. The honest summary

**What works well:**
- Test suite passes with zero skipped, catches most regressions automatically.
- Guardrail tests turn user-stated rules into enforced contracts. Same-class bugs can't ship twice.
- Disaster recovery is rehearsed and verified — not theoretical.
- Defense-in-depth controls (kill switch, dup guards, reconcile, aggregate audit, `pending_fill`) catch what slips past tests.

**What still requires human review:**
- New strategy proposals need eyeballing (the rigorous backtest gauntlet helps, but its gates can be argued with).
- Guardrails enforce **structure**, not **correctness** — a test that says "every endpoint is admin-gated" doesn't say "the admin's settings are correct."
- The AI assistant CAN be asked to do something the rules say not to. The user is the final check on intent.

**What's documented as a known limit:**
- See `docs/01_EXECUTIVE_SUMMARY.md` "What's honest about the limits" + every module's `**Honest limits:**` block.

---

## See also

- `docs/07_OPERATIONS.md` — deploy, monitoring, restore runbook in operational detail.
- `docs/08_RISK_CONTROLS.md` — every risk gate enumerated.
- `docs/10_METHODOLOGY.md` — the engineering principles these guardrails encode.
- `docs/11_INTEGRATION_GUIDE.md` — how to add new code without violating the guardrails.
- `CHANGELOG.md` — the institutional memory.
