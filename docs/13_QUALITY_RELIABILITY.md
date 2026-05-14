# 13 — Quality, Safety, and Reliability

**Audience:** the operator, anyone reviewing whether the system is trustworthy enough to put money on, and any future AI assistant editing this codebase.
**Purpose:** the current strategy for keeping the platform safe and reliable — test discipline, AI-behavior guardrails, in-flight production controls, backup and recovery. This document describes what is in place today; the history of changes lives in `CHANGELOG.md`.

---

## 0. Why this document exists

QuantOpsAI ships changes daily, often via AI-assisted edits. The threat model is not just "does the code compile" — it includes "did the assistant hallucinate a column name?", "did the assistant remove a guard while fixing something else?", "did the assistant claim a fix is done when it isn't?". The safety system has three layers, all enforced automatically:

1. **Pre-commit / CI tests** (274 files, 3,059 tests, zero skipped) — must pass before merge.
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

| Guard | Catches |
|---|---|
| `test_no_snake_case_in_user_facing_ids` | Templates rendering raw `snake_case` for sectors, factors, scenarios, or PARAM_BOUNDS keys. |
| `test_no_snake_case_in_api_responses` | API responses leaking PARAM_BOUNDS keys to the JS layer. |
| `test_api_response_values_no_snake_case` | **Broader.** Walks every `/api/<route>/1` GET response and fails on any string VALUE matching the snake_case pattern in a non-allowlisted field. Catches the case where an API returns a raw enum value (e.g., `"insufficient_history"`) that the JS renders directly. The labeled-list shape (`{name, label, ...}`) is recognized — `name` is the form-action key, `label` is what's rendered. Allowlist `INTERNAL_VALUE_FIELDS` covers fields whose values are switch/case codes for JS (`regime`, `prediction_type`, `side`, `status`, etc.). |
| `test_no_internal_leakage_in_templates` | Blanket static scan over every `templates/*.html`. Fails on `(Item Nx)` / `(OPEN_ITEMS X)` internal-tracker references AND on raw snake_case in visible text. Allowlist starts EMPTY by design. |

The repeated user complaint: *"never ship raw snake_case in the UI."* These tests make that the default-deny posture.

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

### 4.3 Reconcile + audit (every 15 minutes via cron)
- **`reconcile_journal_to_broker`** — compares every per-profile journal against Alpaca. Detects: phantom SELLs (logged but not filled), partial-sale drift, broker-side liquidation, canceled entries that the journal still claims as open. Auto-corrects by undoing phantoms and backfilling broker actions.
- **`aggregate_audit`** — sums virtual positions across profiles routing to the same Alpaca account, compares to `api.list_positions()`. Catches multi-profile-overshoot scenarios where the sum exceeds broker actuals (logic bug).

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

The full procedure has been rehearsed end-to-end on production with bit-identical recovery. Step-by-step is in `Docs/07_OPERATIONS.md` §9.

### 5.3 What the system does NOT have (honest limits)
- No off-site backup replication. The 14-day local snapshots are the only copies.
- No formal RPO / RTO commitment. RPO is ~24h (last daily snapshot); RTO is minutes-to-hours (manual restore command).
- No multi-tenant isolation (single-operator design).

---

## 6. Pre-deploy verification (`verify_first_cycle.sh`)

After every deploy, run `./verify_first_cycle.sh` from the repo root. The script runs ~30 checks against the live droplet:

- **§0 Service health** — both services active, gunicorn workers fresh, prod git matches origin/main.
- **§A Core scheduler health** — no NameError on Check Exits, per-position failures stay local, zero TASK FAILs since the latest fix-deploy cutoff.
- **§B Cost & quality levers** — persistent cache hits, meta-pregate firing, disable-list reaching ensemble.
- **§C INTRADAY_STOPS_PLAN** — broker stops actively placed (≥80% coverage), polling defers to broker trailing, MFE populated.
- **§D Long/short capability** — short emission, regime-gate handling, RS universe candidates, Phase-1 validator clean, Kelly recommendations.
- **§E Trade-quality metrics** — scratch classification, MFE capture, signed slippage cost.
- **§F Trade execution** — loud-logged rejections classified, track record populated, pending-orders filtered.
- **§G Cost** — today's spend.
- **§H Alt-data** — bundled DBs at the merged path, refresh within 30h, cron entry correct.
- **§I PDUFA + AdComm** — table populated, drug names extracted, idempotency table.
- **§J UI guardrails** — no internal-tracker refs, no snake_case in dropdowns, blanket guardrail green, options-backtest endpoint working, display-name tests passing.

Each check has a per-fix deploy cutoff so historic pre-fix failures don't show as current. Adding a new fix = bump the cutoff in the script.

---

## 7. CHANGELOG and memory discipline

### 7.1 CHANGELOG
- Every commit that changes behavior gets an entry.
- Format: `## YYYY-MM-DD — short title (Severity: X, type)` with what broke, why it wasn't caught, what the fix does, why the new test would catch it next time.
- Enforced by `test_recent_py_commits_paired_with_changelog`.
- File: `CHANGELOG.md` at repo root — the project's institutional memory. **Date-stamped incident detail belongs here, not in this docs tree.**

### 7.2 Auto-memory (Claude assistant)
- File-based memory at `/Users/mackr0/.claude/projects/-Users-mackr0/memory/`.
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
- See `Docs/01_EXECUTIVE_SUMMARY.md` "What's honest about the limits" + every module's `**Honest limits:**` block.

---

## See also

- `Docs/07_OPERATIONS.md` — deploy, monitoring, restore runbook in operational detail.
- `Docs/08_RISK_CONTROLS.md` — every risk gate enumerated.
- `Docs/10_METHODOLOGY.md` — the engineering principles these guardrails encode.
- `Docs/11_INTEGRATION_GUIDE.md` — how to add new code without violating the guardrails.
- `CHANGELOG.md` — the institutional memory.
