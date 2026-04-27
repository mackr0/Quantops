# Changelog

Every bug fix, behavior change, and known-issue resolution. Newest entries
at the top. Each entry includes the problem, root cause, fix, and any
follow-up work tracked separately.

**Format**: YYYY-MM-DD — short title. Severity: critical / high / medium / low.

Rules going forward:
- Every production bug fix gets an entry here before deploy
- Every fix must name the test that prevents regression (or a follow-up TODO
  to add one)
- "Production" means anything that changed behavior on the droplet, not
  code-only refactors
- Honest failure analysis: **what broke**, **why it wasn't caught**, **what
  the fix actually does**, **why the new test would catch it next time**

---

## 2026-04-27 — Wave 1 / Fix #6: forward-horizon gate on prediction resolution (Severity: medium-going-on-critical, accuracy)

Wave 1 of `METHODOLOGY_FIX_PLAN.md` is now complete (Fix #2 + Fix #6).

**Before:** `ai_tracker._resolve_one` checked the ±2% win/loss
thresholds against the current price as soon as the next resolve-tick
ran. A BUY made at 10am that drifted +2.5% by 11am resolved as
"win" within an hour — the label captured intraday noise, not the
forward outcome the AI was actually predicting. With a 2% threshold
and typical retail-cap volatility (small-caps routinely move ±2%
intraday on no news), a meaningful fraction of resolved labels were
random.

**After:** new constant `MIN_HOLD_DAYS_BEFORE_RESOLVE = 5` (5 trading
days ≈ 1 trading week). `_resolve_one` returns `None` (still pending)
for any BUY/SELL prediction younger than that, regardless of price
movement. After the horizon, the same threshold logic runs and the
prediction resolves to win/loss. HOLD's existing `HOLD_RESOLVE_DAYS`
gate is preserved (already had this discipline). `TIMEOUT_DAYS`
escape hatch still force-resolves stale pending predictions to
neutral.

**Effect on observable metrics:**

- Pending count climbs temporarily as young predictions wait their
  horizon out (instead of resolving immediately on noise).
- Win rate on freshly-resolved predictions becomes a meaningful
  forward-horizon measurement instead of a noise estimate.
- The meta-model's training labels (which feed off resolved
  predictions) become more predictive — combined with the
  time-ordered split fix from `cd2d207`, this is the second of
  two changes that determine whether the meta-model has any real
  edge to learn.

**Anti-regression — `tests/test_resolve_min_hold_horizon.py` (10 tests):**

1. Constant exists and is ≥ 1.
2. Source-level: `_resolve_one` references the constant.
3. Young BUY at +2.5% returns None (was: "win").
4. Young BUY at -2.5% returns None (was: "loss").
5. Young SELL at -3% returns None.
6. Aged BUY at +3% resolves as "win" (gate doesn't block real wins).
7. Aged BUY at -3% resolves as "loss".
8. HOLD path preserved — too-young HOLD stays pending.
9. HOLD path preserved — past-horizon HOLD with quiet price resolves win.
10. TIMEOUT escape hatch — old pending BUY with no threshold cross
    still force-resolves to neutral.

Tests: 975 passing (was 965; +10 new).

**Wave 1 status:** ✅ COMPLETE.
- Fix #2 (backtest_strategy date ranges) — `a3a3d64`
- Fix #6 (forward-horizon resolution gate) — this commit

**Wave 2 starts next:** rewire `walk_forward_analysis` and
`out_of_sample_degradation` to use the new date-range path, then
add the train/validate split to `self_tuning`.

---

## 2026-04-27 — Wave 1 / Fix #2: backtest_strategy accepts explicit date ranges (Severity: critical, accuracy)

Foundation for the methodology fix. Wave 1 of `METHODOLOGY_FIX_PLAN.md`.

**Before:** `backtest_strategy(market_type, days=N, ...)` always
fetched the latest N days from `datetime.now()`. Every wrapper that
called it (walk_forward_analysis, out_of_sample_degradation, plus
any future caller wanting "historical period X") inherited the
"all windows end at today" defect.

**After:** `backtest_strategy` now also accepts `start_date` and
`end_date` parameters. When both are passed, simulation reads
EXACTLY the bars in `[start_date, end_date]`, with warmup from
`start_date - 80 calendar days` for indicator priming. The
sim-loop's start index is the first bar at or after `start_date`,
so bars before it are warmup and bars after `end_date` are
ignored.

**New helper `backtester._fetch_yf_history_range(symbol, start, end,
warmup_days)`** is the date-range counterpart to
`_fetch_yf_history(symbol, days)`. Slices the cached full-history
dataframe by date instead of row count. Tz-aware against tz-naive
indices. Returns None when the requested range is outside cached
data.

**Backwards compat:** `days=` parameter remains accepted as the
legacy entry point. Positional-argument order preserved (`days`
ahead of `start_date` in the signature) so no existing caller
breaks. Wave 2 fixes (#3, #4) will migrate walk_forward_analysis
and out_of_sample_degradation to the date-range path.

**Anti-regression — `tests/test_backtest_date_range_split.py` (6 tests):**

1. Public API has `start_date` and `end_date` parameters.
2. `_fetch_yf_history_range` helper exists.
3. Slicing returns bars inside the requested window plus warmup.
4. Out-of-cache windows return None gracefully.
5. **The leakage detector:** two backtests with disjoint date
   ranges read disjoint simulation bars (the property
   walk-forward and OOS depend on).
6. Legacy `days=` path still works and parameter order is
   preserved for positional-arg compat.

Tests: 965 passing (was 959; +6 new).

**Next:** Fix #6 (ai_tracker forward-bar resolution) completes
Wave 1. Then Wave 2: rewire walk_forward_analysis and
out_of_sample_degradation to use the new date-range path.

---

## 2026-04-27 — METHODOLOGY_FIX_PLAN.md: durable plan for the 7 remaining accuracy bugs (Severity: low, docs)

After the meta-model data-leakage fix landed (`cd2d207`), the user
asked: "are there other aspects of this system that are equally
incorrect or inaccurate?" An Explore-agent audit (assistant verified
the top 3 findings personally) surfaced 7 issues sharing the same
root pattern: wrappers around `backtester.backtest_strategy()` use
`days=N` parameters that always fetch from `datetime.now()` backwards,
so every "walk-forward" / "out-of-sample" / "in-sample" test reads
overlapping recent data. Plus `self_tuning` optimizes parameters on
full history, predictions resolve on same-day close, alpha-decay
windows have forward-looking bias, and specialist confidence is
never calibrated against actual outcomes.

`METHODOLOGY_FIX_PLAN.md` documents:

- The full inventory of 7 issues with severity, file, line range,
  and brief description.
- A 3-wave dependency graph: Wave 1 (`backtest_strategy` date ranges
  + forward-bar resolution) is structural foundation; Wave 2
  (walk-forward, OOS, self-tuning hold-out) becomes mechanically
  correct once Wave 1 ships; Wave 3 (alpha-decay discipline,
  lifecycle gates, specialist calibration) consumes the clean data
  produced by 1+2.
- Per-fix execution plan: implementation, anti-regression test,
  migration, expected metric impact.
- Honest expected-impact table — meta-model AUCs probably drop to
  0.50-0.65, validation reports become more sobering, self-tuner
  applies fewer changes, alpha-decay flags more strategies. Calibrated
  numbers are the goal.
- Cross-session continuity rules so this plan survives context loss.

User instruction was explicit: "we need to do it all." Wave 1 starts
in the next commit.

---

## 2026-04-27 — Meta-model: fix data-leakage from random train/test split (Severity: critical, accuracy)

**The problem we found.** Per-profile dashboard reported AUC values
of 0.83-0.96 across every profile. Realistic out-of-sample financial
AUCs are ~0.55. The numbers were not real edge — they were a known
data-leakage artifact.

**Root cause.** `meta_model.train_meta_model` was using
sklearn's `train_test_split(X, y, test_size=0.2, random_state=42)`
— a RANDOM 80/20 split with no time awareness. Test predictions
were interleaved in time with training predictions. Because
financial features are heavily autocorrelated day-to-day (RSI today
≈ RSI tomorrow, regime today ≈ regime tomorrow), the classifier
effectively memorized "this market state ≈ this outcome" instead
of learning predictive patterns. AUC inflated from a realistic
~0.55 to an artifact ~0.95.

Compounding it: `build_training_set` selected from `ai_predictions`
without an `ORDER BY`. SQLite's row order in that case is
implementation-defined, so even a deterministic slice of the result
would have been random in time.

**Fix:**

1. `build_training_set` query now `ORDER BY id ASC` — guarantees
   time-ascending order. Comment in code references this CHANGELOG
   entry as the reason.
2. `train_meta_model` no longer imports or calls
   `sklearn.model_selection.train_test_split`. Replaced with a
   deterministic tail split:
   ```python
   n_test = max(1, int(round(n * 0.2)))
   n_train = n - n_test
   X_train, X_test = X[:n_train], X[n_train:]
   y_train, y_test = y[:n_train], y[n_train:]
   ```
   The most-recent 20% becomes the held-out test set. No shuffling.
   No `random_state` on the split. (Classifier `random_state=42` is
   kept — that's reproducibility, not data leakage.)

**Honest expectation.** AUCs will drop on the next retrain, possibly
significantly. A drop from ~0.95 to ~0.55-0.65 would be GOOD news —
that's a real edge, just much smaller than the leakage made it look.
A drop to ~0.50 means the AI's confidence has no learnable
correction from these features and we'd need to either widen the
feature set or accept raw AI confidence. Either outcome is more
useful than continuing to operate on inflated numbers.

The user's explicit guidance: "yes, accuracy above all else."

**Anti-regression — `tests/test_meta_model_time_ordered_split.py` (4 tests):**

1. `test_train_meta_model_does_not_import_train_test_split` —
   AST-walks `train_meta_model` source; fails the build if anyone
   reintroduces sklearn's random splitter.
2. `test_build_training_set_orders_by_id_asc` — regex-asserts the
   query has `ORDER BY id ASC` (or `ORDER BY timestamp ASC`).
3. `test_train_meta_model_uses_deterministic_tail_split` — confirms
   the slice-based split idiom is present.
4. `test_split_takes_most_recent_data_as_test_set` — behavioral
   end-to-end: feeds 100 samples where the LAST 20 deliberately
   invert the training pattern. With the time-ordered split, AUC
   on test data must be ≤ 0.5 (because the test half contradicts
   what the model learned). With a random split, the inverted
   samples interleave into training and AUC would stay artificially
   high. This test is the actual leakage detector.

Tests: 959 passing (was 955; +4 new).

**Post-deploy step:** delete `meta_model_*.pkl` files on prod so
the next daily retrain (3:55 PM ET) trains fresh on the corrected
methodology. Dashboard AUCs will reflect reality from that point.

---

## 2026-04-27 — Documented "trade-execution costs modeled at $0" decision (Severity: low, docs)

User reviewed today's trailing-stop exits (mostly profitable; AMD
+$190, NXPI +$224, QCOM +$53; one stop-loss on TXN -$99) and asked
why the system doesn't subtract per-trade commissions. Combined
recall (his E*Trade account didn't charge him) with current market
reality (every major US retail broker — Alpaca, Schwab, Fidelity,
E*Trade, IBKR Lite, Robinhood, Charles Schwab — has been $0 stock
commission since 2019) and the existing slippage-tracking that
already captures the only material trade-cost (bid-ask spread).

Result: trade execution costs stay modeled at $0; decision is now
documented in `TECHNICAL_DOCUMENTATION.md` §15 ("Cost Model" → new
"Trade Execution Costs" subsection) so the reasoning is preserved
if anyone questions it later.

The single small gap — short-borrow fees on overnight shorts — is
explicitly noted as deferred (small magnitude; rarely held >1-3
days; clean post-hoc add when a >5-day short shows up in the
journal).

---

## 2026-04-27 — check_exits: skip exits whose entry order hasn't filled at the broker (Severity: medium, bug)

**Symptom:** Production scan-failures widget showed
`Large Cap Limit Orders: [Large Cap Limit Orders] Check Exits failed
at Apr 27, 1:53 PM ET`. Stack trace from journal:

```
alpaca_trade_api.rest.APIError:
    cannot open a short sell while a long buy order is open
```

**Root cause:** Virtual profiles compute "open positions" from the
trades journal as soon as the entry order is logged — even before
Alpaca actually fills it. For most profiles this is fine because
their entry orders are market orders that fill in milliseconds. But
"Large Cap Limit Orders" places limit BUYs that can sit unfilled at
Alpaca for minutes or hours.

Sequence that broke:

1. 17:50 — limit BUY for symbol X submitted, journal records an
   open virtual position.
2. 17:53 — `check_exits` runs, sees the journal-derived position,
   detects a stop-loss/take-profit trigger, submits a market SELL.
3. Alpaca: "you have 0 real shares (the BUY hasn't filled) AND
   there's still a long BUY pending — this SELL is a short
   attempt — rejected." Task fails.

The existing defense at `trader.py:281-292` (cancel any open orders
for this symbol before submitting the exit) didn't help because the
cancel hits Alpaca asynchronously; the submit fired before the
cancel landed.

**Fix (`trader.py`):**

New helper `_entry_order_filled_at_broker(api, db_path, symbol,
is_short)` looks up the most recent matching open entry row in the
journal, reads its `order_id`, calls `api.get_order(...)`, and
returns:

- `True` if status is `filled` or `partially_filled` (real shares
  exist → SELL is safe).
- `False` for any pending state (`new`, `accepted`, `pending_new`,
  `pending_replace`, `pending_cancel`, `accepted_for_bidding`,
  `held`, `suspended`).
- `True` (fail-open) on every uncertain path: missing db_path, no
  matching journal row, NULL order_id, broker-unrecognized id, or
  SQL error. Reason: a too-conservative gate would block legitimate
  exits when the journal is healthy but its row→Alpaca link is
  stale; the prior behavior was "always allow," so fail-open is the
  conservative regression-free choice.

`check_exits` now calls this gate immediately after the schedule
guard. If `False`, it logs an INFO line and continues — the trigger
re-fires on the next exit cycle, by which time the entry has
typically filled.

**Effect on the failing profile:** the limit-order profile no longer
errors on exits during the entry-pending window. Alpaca-state is
now the source of truth for "does this position really exist?", not
the optimistic journal.

**Anti-regression — `tests/test_exit_gates_unfilled_entry.py` (18 tests):**

- `filled` and `partially_filled` allow the exit.
- All 8 known pending Alpaca statuses block the exit (parameterized).
- Short positions: `sell_short` entry side is looked up correctly,
  and pending shorts block the cover.
- All 5 fail-open paths return `True`: no db_path, no matching row,
  NULL order_id, broker raises on `get_order`, SQL error.
- **Contract test** uses `inspect.getsource(check_exits)` to assert
  the gate call is still present in `check_exits` itself — prevents
  a silent regression where someone removes the wiring but leaves
  the helper.

Tests: 955 passing (was 937; +18 new).

---

## 2026-04-27 — Show current price + % change inline on position rows (Severity: low, ui)

User asked to see current price on the dashboard without having to
click-expand each position row. The data was already in the row dict
(`current_price` from Alpaca, used for unrealized P&L) and was already
rendered — but only inside the click-to-expand detail panel.

`templates/_trades_table.html`: the Price column now stacks the entry
price (top) with the current price + % change (below, color-coded
green/red). Renders only when `current_price > 0` so closed/SELL rows
on the trades page don't grow a redundant line. The duplicate
"Current: ..." line in the expanded detail panel was removed since
it would just repeat what's now visible in the main row.

Zero new system load — uses the same data already fetched for the
P&L calc.

**Follow-up fix same day:** the first cut naively did
`(current - entry) / entry` regardless of side, which would have
shown a SHORT position GAINING when the underlying price ROSE
(opposite of reality). Caught while no shorts were open in prod, so
the bug never bit. Fix inverts the sign for `side in ('sell',
'sell_short', 'short')`. Guardrail: `tests/test_trades_table_pnl_sign.py`
covers long winner, long loser, short winner, short loser, the
dashboard's `side='sell'` alias for shorts, and the closed-trade
no-render case (6 tests).

---

## 2026-04-27 — Dashboard rate-limit storm: per-symbol bars → batched snapshots (Severity: critical, regression-prevention)

**Symptom:** Monday's market open. User reports dashboard "loading for
7 minutes" — looks broken. Gunicorn logs:

```
13:42:36 sleep 3 seconds and retrying https://data.alpaca.markets/v2/stocks/GT/bars
13:42:36 sleep 3 seconds and retrying https://data.alpaca.markets/v2/stocks/ET/bars
13:43:50 [CRITICAL] WORKER TIMEOUT (pid:903832)
13:43:51 [ERROR] Worker (pid:903832) was sent SIGKILL!
```

**Root cause:** `client._make_price_fetcher` called
`market_data.get_bars(symbol, limit=1)` once per symbol. Virtual
profiles use this fetcher to compute current prices for FIFO-derived
positions. Math:

  10 virtual profiles × 4-8 held positions × ThreadPoolExecutor of 10
  parallel workers = 50-100 sequential per-symbol Alpaca bar requests
  per dashboard render. → Alpaca rate limit. → 3-second-sleep retries.
  → 120s gunicorn worker timeout. → SIGKILL. → next request restarts
  the same trap.

The screener migration to Alpaca SIP (CHANGELOG 2026-04-15) fixed the
*screener's* yfinance hang but left this dashboard path on per-symbol
calls because it was a separate code path under
`client._make_price_fetcher`.

**Fix (`client.py`):**

1. New `_prefetch_prices(symbols)` — one batched
   `data_client.get_snapshots(symbols)` call (the same path the screener
   uses) populates a process-wide TTL price cache (30s).
2. `_make_price_fetcher` now reads from that cache; per-symbol fallback
   to `api.get_latest_trade` only fires for the rare cache miss (e.g.
   delisted ticker).
3. Module-level `_price_cache` dict + `_price_cache_lock` so concurrent
   gunicorn workers in the same process share the cache.
4. New `_held_symbols_from_journal(db_path)` reads the symbol list from
   the trades table so callers can prefetch BEFORE invoking the journal
   helper.
5. Both `get_account_info` and `get_positions` now call
   `_prefetch_prices(_held_symbols_from_journal(ctx.db_path))` before
   passing the fetcher to the journal helper.

**Effect:** Dashboard render goes from N×M Alpaca calls (where N =
profiles, M = symbols/profile) to **1 batched snapshots call per
render**. Result is shared across all profiles via the process cache.

**Anti-regression — `tests/test_no_per_symbol_bars_in_web_path.py`** (5 tests):

1. `test_price_fetcher_does_not_call_get_bars` — AST-walks
   `_make_price_fetcher` and fails if it ever calls `get_bars` again.
2. `test_prefetch_prices_uses_batched_snapshots` — confirms the new
   prefetch uses `get_snapshots`, not `get_bars`.
3. `test_price_fetcher_has_process_wide_cache` — asserts module-level
   `_price_cache`, `_PRICE_CACHE_TTL`, and `_price_cache_lock` exist.
4. `test_dashboard_view_does_not_call_get_bars` — grep guard on
   `views.py`.
5. `test_held_symbols_helper_exists` — ensures the symbol-list helper
   exists for batched prefetch.

The structural test makes it impossible to revert this fix without
the build failing on the exact pattern that caused the outage.

Tests: 931 passing (was 926; +5 new structural tests).


Closing-out doc pass to bring the front-of-repo docs in line with what
actually ships now.

1. **`README.md`** — was still describing the system as it stood ~6
   weeks ago. Refreshed:
   - Top blurb: now names the 4 new alt-data sources and the 12-layer
     autonomy stack instead of "self-tuning adjusts parameters daily".
   - "Self-Tuning" feature bullet replaced with "12-Layer Autonomous
     Self-Tuning" naming the override chain and cost guard.
   - Web Platform list adds the 5 new dashboard widgets that landed in
     the autonomy rollout: Active Lessons, Active Autonomy State, Cost
     Guard, Parameter Resolver, Autonomy Timeline.
   - "All 105 tests" → "All 926 tests".
   - New §6 setup step documents the alt-data wiring (clone, `daily`,
     `~/run-altdata-daily.sh`).
   - Project Structure tree expanded with new groups: Phase 1-10
     module additions (`meta_model`, `alpha_decay`, `options_oracle`,
     `ensemble`, `event_bus`, `crisis_detector`, etc.) and a new
     "Autonomy Layer" group naming all 10 modules.
   - Documentation list lifted from "TECHNICAL_DOCUMENTATION.md (v4.0)"
     to a full enumeration including `EXECUTIVE_OVERVIEW`, `ROADMAP`,
     `AI_ARCHITECTURE`, `SELF_TUNING`, `AUTONOMOUS_TUNING_PLAN`,
     `ALTDATA_INTEGRATION_PLAN`, `MONTHLY_REVIEW`, `CHANGELOG` and
     bumps the TECHNICAL_DOCUMENTATION reference to v5.0.

2. **`ROADMAP.md`** — replaced the "Upcoming Enhancements (Queued):
   Self-Tuning Parameter Expansion (~Late May 2026)" section, which
   claimed the self-tuner adjusts 4 parameters and 3 more were queued
   for a month from now. That plan was superseded a week early by the
   12-wave rollout. Section now reads "✅ DELIVERED (2026-04-25)"
   with the full layer table, override-chain explanation, and the
   6 anti-regression guardrails. Also added a parallel "Alternative
   Data Integration ✅ DELIVERED (2026-04-26)" section so the roadmap
   reflects what shipped this weekend. Bumped baseline test count in
   cross-session continuity from 104+ → 920+.

3. **`ALTDATA_PLAN.md`** — added a "STATUS: ✅ DELIVERED" banner at
   the top pointing to `ALTDATA_INTEGRATION_PLAN.md` as the live
   integration design, and clarified that the document is preserved
   as the historical record of the project-build plan rather than a
   living roadmap.

Tests: 926 passing (no .py change in this commit; documentation-only).

---

## 2026-04-26 — Alt-data integration: doc completeness pass (Severity: low, docs)

End-of-session sweep: tests/docs/UI/prod-logs audit caught three
documentation gaps from the alt-data integration session:

1. `AI_ARCHITECTURE.md` had a count bump (15 → 19 alt-data signals)
   but didn't actually describe the 4 new sources or list them in
   the file map. Added an explicit table under §1c naming each helper,
   its source project, and per-symbol output. Added the
   `/opt/quantopsai-altdata/` path to the §6 file map.
2. `SELF_TUNING.md` bumped the count (21 → 25 weighted signals) but
   didn't enumerate which 4 were new. Added a complete grouped table
   of all 25 weightable signals with the 4 alt-data additions called
   out.
3. `ALTDATA_INTEGRATION_PLAN.md` still said "Plan draft, ready for
   execution" — flipped to "DEPLOYED 2026-04-26" with the verified
   record counts (1,109 trades / 857,304 holdings / 5,342 trials /
   981 messages).

Helper docstrings in `alternative_data.py` updated to call out the
prod path (`/opt/quantopsai-altdata`) and the daily cron schedule —
makes the runtime contract clear to future readers.

Tests: 925 passing (was failing on the CHANGELOG-discipline rule
because the W1+W2 .py-touching commit didn't include CHANGELOG; this
follow-up commit bundles `.py` + `CHANGELOG.md` + docs together,
re-satisfying the rule going forward).

---

## 2026-04-26 — Alt-data integration: 4 standalone projects wired into the AI (Severity: medium, feature)

The four projects built last week — `congresstrades`, `edgar13f`,
`biotechevents`, `stocktwits` — are now feeding the AI's prompt as
weighted signals on the same Layer 2 ladder as everything else.

**W1 — Read layer** (`alternative_data.py`): four new helpers
(`get_congressional_recent`, `get_13f_institutional`,
`get_biotech_milestones`, `get_stocktwits_sentiment`) read each
project's SQLite DB read-only with 6h cache, configurable path via
`ALTDATA_BASE_PATH`. Graceful no-op when DB is missing or schema is
partial. 12 new tests with seeded fixtures mirroring prod schema.

**W2 — AI integration**: 4 new keys in `get_all_alternative_data`,
4 new prompt blocks via `_weighted_signal_text` (so Layer 2 weights
apply), 4 new entries in `signal_weights.WEIGHTABLE_SIGNALS` so the
tuner can autonomously discount any signal that doesn't predict
for a profile. Features flattened into `features_payload` so the
meta-model can train on them too.

**W3 — Production deployment**: 4 projects rsync'd to
`/opt/quantopsai-altdata/{project}/` on the droplet. Fresh venvs +
`pip install -r requirements.txt` per project (~217MB total). Cron
entry at 06:00 UTC (02:00 ET, off hours):
`0 6 * * * cd /opt/quantopsai-altdata && ALTDATA_BASE=/opt/quantopsai-altdata bash run-altdata-daily.sh >> logs/altdata-$(date +%Y%m%d).log 2>&1`.
Driver script patched to honor `ALTDATA_BASE` env var with
`$HOME` fallback for local-dev compat. `ALTDATA_BASE_PATH` added to
`/opt/quantopsai/.env` so the QuantOpsAI services find the DBs at
the right path. Manual seed run kicked off post-deploy.

**W4 — Docs + UI**: "What the AI Sees" reference card on the AI page
now shows the 4 cards as active sources (moved out of "Built Locally
— Not Yet Wired In"). Alt-data source count bumped 15 → 19.
`SELF_TUNING.md` Layer 2 inventory bumped 21 → 25 signals.
`AI_ARCHITECTURE.md` updated.

Each new signal joins the same self-correcting feedback loop as
every other one — if congressional-trade signals don't predict for
a profile, Layer 2 nudges the weight from 1.0 → 0.7 → 0.4 → 0.0
within ~9 days. Layer 5 propagates that finding to peer profiles.
Cost guard wraps prompt verbosity changes from any expanded
signal set.

Full suite: 926 passed (914 + 12 new alt-data reader tests).

---

## 2026-04-25 — Hotfix: Active Lessons widget stuck on "Loading..." (Severity: medium, regression)

**Problem:** The new "Active Lessons" widget on the AI Operations
tab showed "Loading..." indefinitely. Backend was fine — endpoint
returned 200 in ~165ms with valid data — but the widget never updated.

**Root cause:** duplicate DOM IDs. The new "Active Lessons" widget
was assigned `id="learned-patterns-widget"`, which was already used
by an older widget on the Brain tab. `getElementById` returns only
the FIRST match, so my JS updated the Brain-tab widget (not visible
on the Operations tab) and left the Active Lessons widget stuck on
its "Loading..." placeholder forever.

**Fix:** rename the new widget to `id="active-lessons-widget"` and
update the JS to target it.

**Structural fix — `test_no_duplicate_dom_ids.py`.** New guardrail
that walks every template under `templates/`, parses `id="..."`
attributes (skipping `<script>` and `<style>` blocks so JS string
literals don't false-positive), and fails if any ID appears more
than once in the same file. Allowlist supported for legitimate
duplicates (e.g., a partial template intentionally included twice).

Verified by reverting the fix: the test failed cleanly on
`learned-patterns-widget appears 2× — JS getElementById returns only
the first match, second/etc. silently orphaned.`

This is the structural protection against the entire class of
"silently orphaned widget" bugs.

Full suite: 914 passed (913 + 1 new dup-id guardrail).

---

## 2026-04-25 — URGENT: comprehensive snake_case guardrail + autonomy summary in weekly digest (Severity: high, regression + feature)

**The snake_case leak that wasn't supposed to be possible.** User
opened the AI Operations tab and saw raw `options_signal weight 0.7`,
`vwap_position weight 0.7`, `ai_confidence_threshold (bull): 30` in
the new "Active Autonomy State" card. Despite my repeated promises
that the existing `test_no_snake_case_in_optimizer_strings` would
catch this everywhere, **it didn't — because that test only covered
`_optimize_*` function returns inside `self_tuning.py`**. Every new
API endpoint and JS render path I built outside that file was
uncovered.

**Root cause:** the new `/api/autonomy-status` endpoint returned
`signal_weights` / `regime_overrides` / `tod_overrides` /
`symbol_overrides` / `prompt_layout` as dicts-of-dicts whose KEYS
were raw PARAM_BOUNDS column names. The JS rendered them with
`Object.entries(...).forEach(e => render(e[0]))` — leak.

**Fix:**
1. `/api/autonomy-status` now returns labeled-list shapes:
   `[{"key": "options_signal", "label": "Options Flow Signal",
   "weight": 0.7}, ...]`. Server-side `display_name(...)` resolves
   every parameter name + regime/tod label.
2. `/api/resolve-param` now includes `param_label`,
   `current_regime_label`, `current_tod_label`, `final_source_label`
   alongside their raw counterparts.
3. AI Operations tab JS rewritten to consume the labeled fields
   instead of raw keys.

**The real fix — `test_no_snake_case_in_api_responses.py`.** A new
end-to-end guardrail that:
- Discovers every GET `/api/*` endpoint via `app.url_map`
- Hits each one with a mocked logged-in user + profile data seeded
  with overrides on every PARAM_BOUNDS key
- Walks the JSON response recursively
- Fails if any PARAM_BOUNDS key appears as either:
  (a) a dict KEY anywhere in nested structures (the
      Object.entries-render leak pattern), OR
  (b) a string VALUE in a field whose name isn't on the
      `ALLOWED_RAW_KEY_FIELDS` allowlist (param_name,
      parameter_name, change_type, key, field, strategy_type —
      all paired with explicit `*_label` siblings).

Verified the test catches the exact regression by reverting the
fix and re-running — it failed cleanly with all three leak paths
(`regime_overrides`, `symbol_overrides`, `tod_overrides`).

This guardrail is dynamic — every new API endpoint added going
forward is automatically covered. No new endpoint can ship a
PARAM_BOUNDS key as a dict KEY without explicitly bypassing the
test.

**Also: weekly digest gains an Autonomy Activity section.** Renders
right after "This Week at a Glance" and includes:
- counts of parameter tunings, strategy deprecations/restorations,
  auto-strategy lifecycle and crisis transitions (this week)
- snapshot of active overrides across all profiles (signal weights,
  regime/TOD/symbol overrides, profiles with non-default capital
  scale)
- cost-guard status (today's spend, daily ceiling with source label,
  7-day average)
- post-mortem patterns extracted this week with examples

Full suite: 913 passed (912 + 1 new comprehensive guardrail).

---

## 2026-04-25 — User-controllable cost ceiling + Parameter Resolver + Autonomy Timeline (Severity: medium, feature)

Three additions that put the user in control of the autonomy and
make it inspectable.

**1. User-configurable daily cost ceiling.** New
`users.daily_cost_ceiling_usd` column (NULL = auto-compute). When
set, overrides the auto-computed `trailing-7-day-avg × 1.5`. Settings
> Autonomy gains an input field; current ceiling shows up with its
source ("user-set" or "auto") so you always know whether your cap is
authoritative. `cost_guard.daily_ceiling_usd()` honors the user
value when present and falls back to auto-compute otherwise. New
`cost_guard.ceiling_source()` helper exposes the provenance.

**2. Parameter Resolver tool** (AI Operations tab). Pick a profile +
parameter (+ optional symbol) → see exactly how the value resolves
through the override chain *right now*. Shows global default +
each layer that has an override + which one wins, with the final
value highlighted. Also annotates position-size parameters with the
current `capital_scale` multiplier (Layer 9). Backed by new
`/api/resolve-param` endpoint.

This is the "why is the AI behaving this way" debugging tool. When
the system has 4 dimensions of overrides stacked, knowing which one
is winning for a specific (param, regime, TOD, symbol) tuple is
otherwise non-trivial to figure out.

**3. Autonomy Timeline** (AI Operations tab). Per-profile
chronological feed of every autonomous change in the last 30 days:
parameter tunings (with from/to + reason + outcome), strategy
deprecations / restorations, post-mortem patterns extracted. Color-
coded by event type with vertical-rail timeline styling. Backed by
new `/api/autonomy-timeline` endpoint that merges `tuning_history`
(master DB) + `deprecated_strategies` + `learned_patterns`
(per-profile DBs) into a single sorted feed.

This is the "what has the system done autonomously" history view.
The Self-Tuning History table covers parameter tunings; the
timeline includes all event types in one place.

**Tests:** 5 new in `test_cost_guard.py` covering user-set vs
auto-computed ceiling precedence, zero/negative override fallback,
and `ceiling_source` provenance. Full suite: 912 passed.

---

## 2026-04-25 — UI surfaces: cost guard status + active lessons cards (Severity: low, UX)

Two read-only widgets on the AI Operations tab so the new
infrastructure is visible without console-spelunking.

**Cost Guard card.** Shows today's spend vs ceiling, headroom
remaining, trailing-7-day average, with a colored progress bar (green
< 60%, orange < 90%, red ≥ 90%). The explanatory subtitle tells the
user that over-ceiling auto-actions become recommendations, not
silent debits. New `/api/cost-guard-status` endpoint backs it.

**Active Lessons card.** Per-profile breakdown of currently-active
post-mortem patterns and tuner-detected failure patterns —
i.e., everything currently being injected into the AI prompt's
LEARNED PATTERNS section. Profiles with no active lessons render as
"AI is operating on default context — no post-mortem patterns or
strong tuner-detected failure patterns to inject." New
`/api/active-lessons` endpoint backs it (named to avoid colliding
with the older `/api/learned-patterns` paginated endpoint).

Tests: full suite 907 still green (UI changes only; no Python logic
changes).

---

## 2026-04-25 — Closed-loop learning: post-mortems on losing weeks + false-negative tuning + comprehensive AI doc (Severity: medium, feature)

Three additions that turn information into learning:

**1. Losing-week post-mortems (`post_mortem.py`).** Weekly Sunday task
per profile. Triggers when the past 7 days underperformed the
long-term baseline by ≥10pt. Clusters losing predictions by feature
signature, identifies the dominant pattern (e.g., "60% of losses had
insider_cluster=high AND vwap_position=below"), stores it as a
`learned_pattern`. The trade pipeline already injects active patterns
into the AI prompt's `LEARNED PATTERNS` section, so the AI sees the
post-mortem learning at its next decision automatically — no extra
wiring needed.

Storage in a new `learned_patterns` table per profile DB. Only the
most recent post-mortem stays "active" so the prompt isn't drowned
in stale lessons. Idempotency marker
`.post_mortem_done_p<id>.marker` prevents re-fire on restart;
excluded from rsync delete.

**2. False-negative tuner rule (`_optimize_false_negatives`).** Scans
HOLD predictions resolved as `loss` (price moved >2% in 3 days, so
we missed an opportunity). When ≥60% of such misses cluster in the
band just below the current confidence threshold (within 10 conf
points), the threshold is rejecting trades it should be taking —
auto-lower it by 5. Same safety scaffolding as other tuner rules.

**3. AI_ARCHITECTURE.md comprehensive rewrite.** The doc now
exhaustively describes everything the AI does end-to-end: 7 agents
× 13–14 calls per cycle, the decision flow, the 12-layer autonomy
system, the cross-cutting cost guard, the closed-loop learning
surfaces (meta-model, alpha decay, post-mortems, false-negative
analysis), the safety guardrails, the user surfaces, and a
file-by-file map of where each piece lives. Should answer "what
does the AI actually do" without code-spelunking.

**Tests:** 9 new in `test_post_mortem.py` covering pattern
extraction, idempotency, prior-pattern deactivation,
get_active_patterns, and the false-negative trigger conditions
(threshold lowering, floor respect, no-cluster no-op). Full suite:
907 passed.

---

## 2026-04-25 — Post-W13 follow-ups: ai_model_auto_tune toggle + namespaced display names (Severity: low, completion)

Two small but real follow-ups to W13:

1. **`ai_model_auto_tune` opt-in toggle** added — schema column on
   `trading_profiles` (default OFF), Settings UI checkbox with
   explicit copy ("OFF by default, flipping this on can increase API
   spend"), wired into the profile-save form. The toggle is the
   per-profile entry point for future tuner logic that A/B tests AI
   models within the cost guard. The actual A/B tuning code is a
   future expansion of Layer 1; for now the toggle exists so users
   can express intent.

2. **Display names cleaned up for the override-stack namespaced keys.**
   Added explicit prefix labels: `weight` → "Signal Intensity",
   `tod` → "Time of Day", `deprecate` → "Deprecate Strategy",
   `layout` → "Prompt Section", `self_commission` →
   "Self-Commissioned Strategy", `capital_scale` → "Capital Scale".
   Plus a `_is_ticker_like` helper that preserves uppercase ticker
   tokens (`NVDA`, `AAPL`) verbatim instead of title-casing them.
   So `symbol:NVDA:max_position_pct` now reads as
   "Symbol — NVDA — Max Position Size (%)" instead of
   "Symbol — Nvda — Max Position Size (%)". Tested for collision
   with the existing AI-cost-purpose `political_context` label.

898 passed.

---

## 2026-04-25 — Post-W13: scheduled the capital allocator, surfaced the autonomy state UI (Severity: medium, completion)

Three real gaps caught after W13 declared "done":

1. **Layer 9 had no scheduled task.** I built
   `capital_allocator.rebalance(user_id)` in W12 and added the user
   opt-in toggle in W13, but never registered the weekly task that
   actually CALLS rebalance(). Without it, flipping the toggle did
   nothing. Added `_task_capital_rebalance` to `multi_scheduler.py` —
   runs Sundays only, file-based idempotency marker
   (`.capital_rebalance_done.marker`) prevents re-firing on restart.
   Iterates users with `auto_capital_allocation = 1`, calls
   `rebalance(user_id)`, logs results. Marker added to sync.sh
   exclude list so deploys don't wipe it.

2. **No UI surface for active overrides.** Six layers of autonomy
   were running invisibly — signal weights, regime/TOD/symbol
   overrides, prompt layout, capital scale all lived in JSON columns
   nobody could see without sqlite. Added `/api/autonomy-status`
   endpoint that returns one entry per profile with all active
   overrides. AI page Operations tab now has an "Active Autonomy
   State" card rendering them as colored pills (green = capital
   scale up, orange = down, blue = regime overrides, purple = TOD,
   red = per-symbol, brown = prompt verbosity). Profiles with no
   overrides show "all defaults, no autonomous overrides active".

3. **SELF_TUNING.md only documented Layers 1-4.** Added sections for
   Layers 5-9 (cross-profile propagation, adaptive prompt structure,
   per-symbol, self-commission, capital allocation) with the same
   detail level as the Layer 1-4 sections.

Full suite: 898 passed.

---

## 2026-04-25 — Autonomous tuning Wave 13: Final guardrail + Settings UI Autonomy section (Severity: medium, infrastructure)

The closing wave of the autonomous-tuning rollout. Ships the
structural guardrail that prevents future regressions in autonomy
coverage, plus the user-facing Settings page surface for the per-user
opt-in toggles.

**Anti-regression test: `test_every_lever_is_tuned.py`.**
AST-walks the `trading_profiles` schema (CREATE TABLE + ALTER TABLE
migrations) and asserts every column is either:
- Updated by `update_trading_profile()` somewhere in `self_tuning.py`
  (covers direct param-tuning and the dynamic-key strategy-toggle
  pattern via `_STRATEGY_TYPE_TO_TOGGLE.values()`); or
- On the explicit `MANUAL_PARAMETERS` allowlist with a written
  rationale.

The allowlist captures every legitimate exception: secrets, identity,
strategic AI choice (opt-in via `ai_model_auto_tune` planned), schedule,
the override-stack JSON storage columns (tuned via layer-specific
helpers, not `update_trading_profile`), boolean execution toggles
(intensity tuned via Layer 2 weights, defaults stay user-set), and
the three placeholder optimizers awaiting feature columns
(avoid_earnings_days, skip_first_minutes, trailing_atr_multiplier).

A second test (`test_no_stale_entries_in_manual_allowlist`) catches
allowlisted-but-no-longer-existing columns so the list stays honest.

**Settings page Autonomy section.** New `<h2 id="autonomy">Autonomy</h2>`
block with a checkbox for `auto_capital_allocation` (default OFF).
The accompanying copy explains the per-Alpaca-account constraint
explicitly so the user understands what they're enabling. New POST
endpoint `/settings/autonomy` persists the toggle to the user record.

**Tests:** 2 new in `test_every_lever_is_tuned.py`. Full suite: 898
passed.

This closes the 12-wave plan. Final state of the autonomous-tuning
system as of 2026-04-25:

- 35+ parameters auto-tuned with cooldown/reversal/bound-clamping
- 21 weighted signals + per-profile intensity ladder
- Per-regime / per-time-of-day / per-symbol overrides chained at
  every decision point
- Cross-profile insight propagation from improvements
- Adaptive AI prompt structure with cost gating
- Self-commissioned new strategies via Phase 7 generator
- Auto capital allocation (opt-in, per-Alpaca-account constrained)
- Cost guard wrapping every spend-affecting action
- Six anti-regression guardrails:
  1. `test_no_recommendation_only` — every Recommendation: string
     must be on a written-rationale allowlist
  2. `test_no_snake_case_in_optimizer_strings` — optimizer return
     strings can't embed raw column names
  3. `test_self_tune_task_no_change_path` — the no-change branch
     can't NameError
  4. `test_signal_weights_lifecycle` — weight ladder + tuner +
     prompt builder
  5. `test_regime_overrides` / `test_tod_overrides` /
     `test_symbol_overrides` — chain precedence
  6. `test_every_lever_is_tuned` — every schema column is
     autonomous or explicitly manual

---

## 2026-04-25 — Autonomous tuning Wave 12: Layer 9 Auto Capital Allocation — opt-in (Severity: medium, behavior)

The final functional layer. When the user flips
`auto_capital_allocation` ON for their account, a weekly task
rebalances per-profile `capital_scale` multipliers based on each
profile's risk-adjusted recent returns. The trading pipeline reads
`capital_scale` before sizing, so a profile at 0.5 takes
half-position-size relative to its own baseline. Default OFF.

**Critical constraint respected:** profiles are virtual on top of
shared Alpaca paper accounts. Multiple profiles can share one real
$1M paper account. The allocator works **per-Alpaca-account**:

1. Profiles are grouped by `alpaca_account_id`.
2. Within each group, scales are normalized so they sum to N (the
   group size). Average stays 1.0; relative shifts move toward
   higher-scoring profiles.
3. Group conservation means the underlying real account is never
   over-committed — if scale[A]=1.5, then scale[B]+scale[C]=1.5 in
   the same group.
4. **Solo profiles** (1 per account) always get `scale=1.0`. There's
   nothing to rebalance against.

**Bounds (in addition to group conservation):**
- Per-rebalance: each scale moves at most ±50% per week.
- Absolute: scale ∈ [0.25, 2.0] — no profile drops below 25% or
  rises above 200% of baseline.

**Schema:** `users.auto_capital_allocation` boolean (default OFF) +
`trading_profiles.capital_scale` REAL (default 1.0). Both
auto-migrated.

**Pipeline integration** (`trade_pipeline.execute_trade`): after the
override-chain resolution of `max_position_pct`, the result is
multiplied by `capital_scale`. So the auto-allocator's decisions stack
on top of all other tuning layers — per-symbol stop-loss × regime ×
TOD × global × `capital_scale` = final position size.

**Tests:** 7 new in `test_capital_allocator.py`: solo-profile
preservation, group-sum conservation, score-weighted shifts, mixed
solo/shared groups, per-rebalance and absolute bound enforcement,
opt-in gate respected. Full suite: 896 passed.

This closes the 9-layer plan from `AUTONOMOUS_TUNING_PLAN.md`. The
last wave (W13) is the cross-cutting guardrail: a test that walks
`trading_profiles` schema and asserts every column is either tuned
or on a manual allowlist. Then the user-facing Settings UI for
opting into the per-user toggles (`auto_capital_allocation`,
`ai_model_auto_tune`).

---

## 2026-04-25 — Autonomous tuning Wave 11: Layer 8 Self-Commissioned New Strategies (Severity: medium, behavior)

The tuner can now identify *gaps* in current strategy coverage and
trigger Phase 7's strategy generator with a focused brief. Heavily
cost-gated (LLM tokens cost real money) and rate-limited to ≤1 per
profile per week.

**Detection** (`_optimize_commission_strategy`): scans the last 30
days of resolved AI predictions. Counts winning BUY/SELL predictions
where `strategy_type` was empty/null — i.e., the AI made the right call
but no existing strategy fired on that pattern. ≥5 such gaps trigger
the commission flow.

**Cost guard**: every commission call is wrapped in
`cost_guard.can_afford_action(user_id, ~$0.05)`. If it would push spend
over the daily ceiling, the gap surfaces as
`Recommendation: cost-gated` instead of firing the LLM.

**Brief construction**: builds a focused prompt for
`strategy_proposer.propose_strategies` describing the gap — sample
symbols, average return — and asks for 1-2 new strategy specs. The
returned specs flow through the existing Phase 7 pipeline:
proposed → validated → shadow → active.

**Rate limit**: 7-day cooldown via the existing
`_get_recent_adjustment` machinery, keyed on `"self_commission"`.
At most one commission per profile per week.

**Tests:** 5 new in `test_self_commission.py` covering insufficient
gaps, cooldown respect, cost-gated path, end-to-end proposal flow,
and empty-proposer-result handling. Full suite: 889 passed.

---

## 2026-04-25 — Autonomous tuning Wave 10: Layer 6 Adaptive AI Prompt Structure (Severity: medium, behavior)

The structure of the AI's prompt — section verbosity per profile —
becomes a tunable surface. The tuner periodically rotates one section's
verbosity across `brief / normal / detailed` to test whether the AI
makes better decisions with different framing. Cost-gated to prevent
verbosity drift toward longer prompts that would balloon API spend.

**New module: `prompt_layout.py`** with sections registry (4 sections
to start: `alt_data`, `political_context`, `learned_patterns`,
`portfolio_state`), parse / get_verbosity / set_verbosity helpers, a
deterministic `pick_rotation` for testability, and an
`estimate_daily_cost_delta` that's used by the cost guard.

**Schema migration:** `prompt_layout TEXT NOT NULL DEFAULT '{}'`
column auto-migrated. Default behavior unchanged — every section is
"normal" until the tuner rotates it.

**Prompt builder integration** (`ai_analyst._build_batch_prompt`):
each tunable section now consults `_verbosity(name)` and adjusts:
- `alt_data` brief = top 3 signals + "(N more)" tail; detailed = same as normal (no extra noise).
- `political_context` brief = 2 lines; normal = 4 (current); detailed = 8.
- `learned_patterns` brief = 2; normal = 5 (current); detailed = 10.

**Tuner rule** (`_optimize_prompt_layout`):
- Requires ≥50 resolved predictions before experimenting.
- 14-day cooldown per rotation (vs 3-day for parameters) so each
  variant has enough cycles to attribute outcomes.
- Cost-saving rotations (toward `brief`) are auto-applied.
- Cost-adding rotations (toward `detailed`) are wrapped in
  `cost_guard.can_afford_action`. If they'd push over the daily
  ceiling, surfaced as `Recommendation: cost-gated` instead.

**Tests:** 18 new in `test_prompt_layout.py` covering parse/get/set,
rotation picking, cost estimation, tuner skip-conditions, cost-gate
auto-apply vs recommend, and end-to-end prompt builder rendering at
brief vs normal verbosity. Full suite: 884 passed.

This is the last "decision-surface" layer before the meta-tuning waves
W11 (self-commissioned strategies) and W12 (capital allocation).

---

## 2026-04-25 — Autonomous tuning Wave 9: Layer 5 Cross-Profile Insight Propagation (Severity: medium, behavior)

When the tuner makes a change that turns out to improve a profile's
win rate (`outcome_after = 'improved'` after the 3-day review window),
the same detection rule now runs against every OTHER enabled profile
belonging to the same user. Each peer's own data has to independently
support the change — no value-copying. The fleet learns ~10× faster
than profiles in isolation, with zero new API spend.

**New module: `insight_propagation.py`.**
- `_peer_profiles(source_id)` — enumerates other enabled profiles in
  the same user's account.
- `_detector_for(change_type)` — maps adjustment types to the
  corresponding `_optimize_*` function in self_tuning.
- `propagate_insight(source_id, change_type, parameter_name)` — for
  each peer, builds a duck-typed context, opens its prediction DB,
  runs the detection rule. Returns a list of human-readable messages
  for peers where the change was applied.

**Integration:** `self_tuning.apply_auto_adjustments` now calls
`propagate_insight` after `review_past_adjustments` finds an
improvement. Propagated changes appear in the tuner's adjustment log
prefixed with `PROPAGATED:` for visibility.

**Critical guarantee — no value-copying.** A change to Mid Cap's
`max_position_pct` doesn't get applied to Small Cap's profile. What
gets propagated is the *detection rule check* — Small Cap's own data
must trigger the same rule before any change is made. Same cooldown,
same reverse-if-worsened, same bound clamping as direct tuning.

**Tests:** 7 new in `test_insight_propagation.py`: detector mapping
coverage, peer enumeration excludes source, no-op-on-unknown-type,
no-op-on-no-peers, end-to-end propagation when peer data triggers,
no-change when peer data is healthy. Full suite: 866 passed.

---

## 2026-04-25 — Autonomous tuning Wave 8: Layer 7 Per-Symbol Parameter Overrides (Severity: medium, behavior)

The most-specific tier of the override stack. Some symbols behave
fundamentally differently from each other — NVDA's optimal stop-loss
isn't KO's. The tuner now creates per-symbol parameter overrides for
symbols with materially different track records than the profile
baseline.

New module `symbol_overrides.py` mirrors the regime/TOD pattern. Schema
column `symbol_overrides TEXT NOT NULL DEFAULT '{}'` auto-migrated.
Symbol keys normalised to uppercase on read/write.

**Tuner detection** (`_optimize_symbol_overrides`): walks symbols with
≥20 individual resolved predictions (high bar — over-fitting risk on
small samples is real) ordered worst-WR-first. Symbols ≥15pt off
overall WR get a per-symbol override. Cooldown 7 days (vs 3 for other
tiers) for the same over-fitting reason. Underperformers get
`max_position_pct` reduced for that symbol; outperformers get
`ai_confidence_threshold` raised.

**Pipeline chain** (`regime_overrides.resolve_for_current_regime`)
extended with optional `symbol=` parameter. Full lookup order is now:

  1. **Per-symbol override** (Layer 7, this wave)
  2. Per-regime override (Layer 3)
  3. Per-time-of-day override (Layer 4)
  4. Profile global value
  5. Caller default

Wired into `trade_pipeline.ai_review` (confidence threshold) and
`execute_trade` (position size, stop-loss, take-profit). Symbol is
already in scope at every call site; passed through to the resolver.

**Tests:** 14 new in `test_symbol_overrides.py` covering parse/resolve
case-normalization, tuner detection (sample-size + threshold
respect), and chain precedence (per-symbol wins over regime when both
set; falls through to regime when no symbol override). Full suite:
858 passed.

The full chain shipped today means parameters can vary along 4
dimensions at once: symbol × regime × time-of-day × global. The tuner
acts on the dimension where the WR signal is strongest. A user with a
profile that has `stop_loss_pct=0.03` could end up with NVDA-in-volatile
at 0.08, NVDA-in-bull at 0.05, regular-symbol-in-volatile at 0.06,
and regular-symbol-in-bull at 0.03 — all autonomously chosen,
all reversible, all bounded.

---

## 2026-04-25 — URGENT hotfix: 100+ daily summary emails sent in a single day (Severity: critical, regression)

**Problem:** User hit their email-sending quota — ~100 daily-summary
emails sent today across ~10 profiles. Root cause: every scheduler
restart re-fired the snapshot bundle (snapshot, summary email, DB
backup, alpha-decay snapshot) because the
`last_run["daily_snapshot"]` flag was in-memory only. Today saw ~10
deploys (W1 + W2 + W3 + 2 hotfixes + W4 + W5 + W6 + this fix), each
restarting the scheduler. 10 restarts × 10 profiles = ~100 daily
summary emails sent for the same calendar day.

**Fix — file-based idempotency markers, like the weekly digest:**
- `_task_daily_summary_email` now writes
  `.daily_summary_sent_p<profile_id>.marker` after sending. Subsequent
  restarts on the same calendar day (ET) skip the send with
  "already sent today".
- `last_run["daily_snapshot"]` now persists to/from
  `.daily_snapshot_done.marker` so the entire snapshot bundle (not
  just the email) doesn't re-fire on restart. Also stops re-running
  expensive daily tasks like alpha-decay snapshot and DB backup.
- Manually pre-created today's markers on prod via SSH so the next
  scheduler tick after this deploy skips today's bundle entirely.

**Why it wasn't caught:** The weekly digest already had this
file-based idempotency pattern (introduced 2026-04 for this exact
reason). The daily summary used in-memory state only — the missing
mirror of the weekly pattern. Tests covered "the email gets sent at
all" but not "the email doesn't get re-sent on restart."

**Also fixed (related):** `RECOGNISED_TODS` and `RECOGNISED_REGIMES`
are sets, so the W5/W6 tuner rules iterated buckets in
hash-randomized order. Tests passed in isolation but failed in the
full suite when the random order picked a different bucket. Fixed
by using explicit ordered tuples for tuner iteration.

---

## 2026-04-25 — Autonomous tuning Wave 7: Cost Guard cross-cutting infrastructure (Severity: medium, infrastructure)

**New module: `cost_guard.py`.** Daily-spend ceiling enforcement that
wraps every autonomous action that could increase API costs. Today's
projected spend (sum of today's actual + the action's estimated extra
cost) is compared against the daily ceiling. If it would push us over,
the action is queued as a "Recommendation: cost-gated" with explicit
cost estimate — the ONLY recommendation prefix the
no-recommendation-only guardrail allows.

API:
- `daily_ceiling_usd(user_id)` — defaults to trailing-7-day-avg × 1.5,
  floored at $5/day so brand-new users aren't immediately blocked.
- `today_spend(user_id)` — sum across user's enabled profile DBs.
- `can_afford_action(user_id, estimated_extra_cost_usd)` — bool gate.
- `format_cost_recommendation(action_summary, user_id, cost)` — the
  standardized "Recommendation: cost-gated — ..." string.
- `status(user_id)` — UI snapshot dict.

**First integration:** the Layer-2 signal-weight nudge-up case (which
re-includes a previously-omitted signal in prompts → longer prompts →
higher API spend per scan). Estimated 1¢/day per re-included signal
at typical scan rate. If the ceiling would be breached, surfaces as
recommendation instead of auto-applying. Future waves (Layer 6
adaptive prompt structure, Layer 8 self-commissioned strategies) will
plug into the same gate.

**Tests:** 11 new in `test_cost_guard.py` covering ceiling computation
(floor + multiplier), can_afford gate (under/over/zero/negative),
recommendation string format, status snapshot. The
`test_no_recommendation_only.py` allowlist gained
`"Recommendation: cost-gated"` with rationale; the staleness check
expanded to scan both `self_tuning.py` and `cost_guard.py`.

Full suite: 844 passed.

---

## 2026-04-25 — Autonomous tuning Wave 6: Layer 4 Per-Time-of-Day Parameter Overrides (Severity: medium, behavior)

Mirror of Wave 5's regime architecture, bucketed by intraday window
(open 09:30-10:30, midday 10:30-14:30, close 14:30-16:00 ET). New
module `tod_overrides.py` with the same shape: `parse_overrides`,
`resolve_param`, `set_override`, `resolve_for_current_tod`. Schema:
`tod_overrides TEXT NOT NULL DEFAULT '{}'` column auto-migrated.

Tuner detection (`_optimize_tod_overrides`): bucket recent resolved
predictions by their timestamp's ET hour, find buckets with WR
divergence ≥12pt from overall, create per-bucket override (reduce
position size in underperforming bucket; raise confidence floor in
outperforming bucket).

Pipeline integration: `regime_overrides.resolve_for_current_regime`
extended to a multi-layer chain — per-regime override beats per-TOD
override beats global. So a profile with `stop_loss_pct=0.03`,
`regime_overrides={"volatile": 0.06}`, and `tod_overrides={"open":
0.05}` resolves to:
- 0.06 in volatile regime (regime wins)
- 0.05 at open in bull regime (TOD fallback)
- 0.03 at midday in bull regime (global fallback)

This is the architectural foundation for Layer 7 (per-symbol overrides)
which will plug into the same chain as the most-specific tier.

**Tests:** 14 new in `test_tod_overrides.py` covering bucket
boundaries, parse/resolve, tuner detection, and chain precedence.
Full suite: 832 passed.

---

## 2026-04-25 — Autonomous tuning Wave 5: Layer 3 Per-Regime Parameter Overrides (Severity: medium, behavior + architecture)

**The big architectural one.** Real quant funds use different
parameters in different market regimes — a stop-loss right for sideways
trading is too tight for volatile breakouts, a position size right in
bull is too aggressive in crisis. This wave gives the tuner a place to
express those overrides without forcing the user to maintain five
copies of every profile.

**New module: `regime_overrides.py`.**
- `RECOGNISED_REGIMES = {"bull","bear","sideways","volatile","crisis"}`
- `parse_overrides(json)` — defensive JSON parsing with bounds
  clamping and unknown-regime/unknown-param filtering.
- `resolve_param(profile, name, regime, default=...)` — single source
  of truth for parameter access at decision time. Per-regime override
  first, then global, then default.
- `resolve_for_current_regime(profile, name, default=...)` — wrapper
  that auto-detects current regime via `market_regime.detect_regime()`
  with 5-minute cache.
- `set_override(profile_id, name, regime, value)` — clamped persist;
  `value=None` removes the override.

**Schema migration:** `regime_overrides TEXT NOT NULL DEFAULT '{}'`
column added to `trading_profiles` via the existing auto-migration
framework.

**Pipeline integration** (`trade_pipeline.py`): every decision-point
read of `ai_confidence_threshold`, `max_position_pct`, `stop_loss_pct`,
`take_profit_pct`, `max_total_positions` now goes through
`resolve_for_current_regime`. Falls back gracefully on any error.

**Tuner detection** (`self_tuning._optimize_regime_overrides`): walks
each regime that has ≥10 resolved predictions. If regime WR diverges
from overall by ≥12pt, creates a regime-specific override:
- Underperforming regime → reduce `max_position_pct` 25% for that
  regime only.
- Outperforming regime → raise `ai_confidence_threshold` +5 to focus
  on strongest setups.

Same safety scaffolding as previous waves: cooldown keyed on
`regime:<regime>:<param>`, reverse-if-worsened, snap to PARAM_BOUNDS.

**Tests:** 17 new in `test_regime_overrides.py` covering parse/resolve
fallback chains, current-regime auto-detection, tuner divergence
detection, sample-size and cooldown respect. Full suite: 818 passed.

**Documentation:** `SELF_TUNING.md` Layer 3 section added.

This is the architectural enabler for per-context decision-making.
Layer 4 (per-time-of-day) and Layer 7 (per-symbol) will reuse the
exact same pattern: a JSON column + a `resolve_for_*` helper +
fallback chain. The pattern generalizes; future context dimensions
just plug in.

---

## 2026-04-25 — Hotfix: sync.sh missed models.py → web restart, schema migration didn't auto-apply (Severity: high, deploy regression)

**Problem:** W4 added a `signal_weights` column to `trading_profiles`
via the auto-migration framework in `models.init_user_db()`, which only
runs at web-server startup (called from `app.py:create_app()`). But
`sync.sh`'s `WEB_PATTERNS` only matched `templates|static|views.py|
display_names.py|app.py|auth.py` — `models.py` wasn't on that list, so
W4 deploy didn't trigger a web restart, and the migration never ran.
Result: every tuner cycle that tried to write a signal weight saw
`UPDATE trading_profiles SET signal_weights=...` fail with `no such
column: signal_weights`. The optimizer's exception was caught by the
orchestrator (so the cycle didn't crash), but the new tuning surface
was effectively dead.

**Fix:**
- Added `models.py` to the `WEB_PATTERNS` regex in `sync.sh` so any
  schema change triggers a web restart on the next deploy.
- Manually ran `init_user_db()` on prod via SSH to apply the missing
  column without a full restart cycle.

**Why it wasn't caught:** Tests don't simulate deploy paths. The
auto-migration framework was assumed to fire on every code push;
the WEB_PATTERNS regex hadn't been updated since the framework was
introduced. Future schema additions to `models.py` now trigger a web
restart automatically.

**Also fixed:** Updated `test_tuning_status_js_uses_real_fields` —
previously `pytest.skip()`-ing because the function was renamed to
`loadTuningStatusPills` during the Self-Tuning widget merge. Test now
asserts hard against the new function name and the actual fields the
pills code uses (`profile_name`, `resolved`, `required`, `can_tune`,
`message`). Suite is now 801 passing / 0 skipped.

---

## 2026-04-25 — Autonomous tuning Wave 4: Layer 2 Weighted Signal Intensity (Severity: medium, behavior + architecture)

**The big one.** Previously every signal the AI saw was binary: present
in the prompt or absent. The tuner could disable a whole strategy via
the toggle pipeline but had no way to express "this signal is weak but
not worthless — discount it." This wave adds per-profile signal weights
on a 4-step discrete ladder (`1.0 → 0.7 → 0.4 → 0.0`).

**New module: `signal_weights.py`** — declarative `WEIGHTABLE_SIGNALS`
list (21 signals to start: insider/options/dark-pool/congressional/
political-context alt-data + modular strategy votes), `WEIGHT_LADDER`
constant, `parse_weights` / `get_weight` / `set_weight` / `nudge_up` /
`nudge_down` helpers. Each signal has an `is_active(features_dict)`
predicate the tuner uses to decide "was this signal materially present
in this prediction" so per-signal WR is computable.

**Schema migration:** added `signal_weights TEXT NOT NULL DEFAULT '{}'`
column to `trading_profiles`. Auto-migration via the existing
ALTER-TABLE-on-startup framework — production profiles get the column
on first restart with no manual DBA work.

**New tuner rule: `_optimize_signal_weights`.** Walks every weightable
signal each cycle, buckets recent resolved predictions by signal
presence, computes differential WR. Nudges DOWN when present-WR ≥10pt
below absent-baseline; nudges UP when present-WR ≥5pt above (recovery).
3-day cooldown per signal keyed on `weight:{signal_name}`.
Reverse-if-worsened protection. Registered as the last entry in the
upward optimizer chain.

**Prompt builder integration** (`ai_analyst._build_batch_prompt`):
introduces a `_weighted_signal_text(name, text)` wrapper around every
`alt_parts.append`. Returns `None` (signal omitted) for weight 0.0;
appends `[intensity 0.4]` for partial weights; passes through unchanged
at full weight. Same logic guards the political-context block.

**Tests (20 new in `test_signal_weights.py`):** parse/snap/round-trip,
nudge ladder edge cases, predicate truthiness, tuner detection
(triggers/doesn't trigger/insufficient-data), and prompt builder
respects each weight tier (full / partial / zero). Full suite: 800
passed.

**Documentation:** `SELF_TUNING.md` Wave 4 section added with the
per-signal ladder, action table, and prompt-builder behavior matrix.
`AUTONOMOUS_TUNING_PLAN.md` Layer 2 marked active.

**System now tunes 35+ levers.** Layer 2 is the architectural enabler
for replacing every binary on/off in the system with graduated weights —
future signals automatically join this system without new schema work.

---

## 2026-04-25 — Hotfix: snake_case parameter names leaked to dashboard ticker via optimizer return strings (Severity: high, UX regression)

**Problem:** User saw `atr_multiplier_tp` in the dashboard activity
ticker. Audit found 13 W1/W2/W3 optimizer functions returning strings
that embedded raw snake_case column names directly:
- `"Tightened atr_multiplier_tp from 3.00 to 2.75"`
- `"Raised min_volume from 500,000 to 750,000"`
- etc.

These strings flow into the activity ticker, weekly digest body, and
tuning-history detail. The `display_names` registry was already correct
for every parameter (`atr_multiplier_tp` → "ATR Target Multiplier") —
the bug was that the registry was never consulted when constructing
these return messages.

**Fix:**
- Added `_label(param_name)` helper in `self_tuning.py` — single
  shortcut to call `display_name()` from inside an f-string.
- Rewrote every offending optimizer return string to use `_label()`.
- Added `tests/test_no_snake_case_in_optimizer_strings.py` — AST-walks
  every `_optimize_*` function in `self_tuning.py`, finds all string
  literals returned, and fails the build if any contains a raw
  parameter name from `PARAM_BOUNDS`. Excludes the legitimate case
  where the parameter name appears as a direct argument to `_label()`
  or `display_name()`. This is now the structural guardrail that
  prevents this class of bug from recurring.

**Why it wasn't caught:** Existing tests verified the tuner WROTE the
right value to the database, but not that the human-readable string
returned to the orchestrator was in plain English. The new test closes
that gap with AST-level enforcement — no future optimizer can ship a
parameter-name leak without explicitly bypassing it.

**Tests:** 780 passed total (1 new guardrail test + label-helper
sanity).

---

## 2026-04-25 — Hotfix: Self-Tune NameError on no-change path (Severity: high, regression)

**Problem:** Production "Scan Failures" panel showed "Self-Tune failed"
for every profile after the first weekend snapshot ran. Root cause:
the earlier "applied vs recommended" notification rewrite moved
`real_changes = applied` inside the `if adjustments:` branch in
`_task_self_tune`. When the tuner found nothing to change (the common
case — most cycles), `real_changes` was never defined, and the
no-changes-needed log path 30 lines below raised `NameError`.

**Fix:** Define `real_changes = applied` unconditionally at the top
of the function, before any branching. Removed the now-redundant
assignment inside the `if` branch.

**Why it wasn't caught:** The original test coverage for
`_task_self_tune` only exercised the changes-applied path. The
no-adjustments path was never hit in tests despite being the most
common production code path.

**Tests:** New `test_self_tune_task_no_change_path.py` with 3 tests:
no-change path (the regression), applied path (sanity), and
recommendation-only path (the new asymmetric branch). Full suite
778 passed.

---

## 2026-04-25 — Autonomous tuning Wave 3: Group B (exit parameters) — 4 new tunable parameters (Severity: medium, behavior)

**4 new exit-parameter tuning rules** (`self_tuning.py`):

| Function | Parameter | Detection |
|----------|-----------|-----------|
| `_optimize_short_take_profit` | `short_take_profit_pct` | Avg short winner < 50% of TP target → tighten 20% |
| `_optimize_atr_multiplier_sl` | `atr_multiplier_sl` | ≥40% of losses cluster near max-loss magnitude (proxy for stops being hit too tight) → +0.25 |
| `_optimize_atr_multiplier_tp` | `atr_multiplier_tp` | Avg winner < 50% of best winner achieved → -0.25 (tighten to capture more) |
| `_optimize_trailing_atr_multiplier` | `trailing_atr_multiplier` | Placeholder until per-trade max-favorable-excursion is tracked |

ATR-multiplier rules respect `use_atr_stops`: skip when off (the
multiplier doesn't apply). Trailing-multiplier rule no-ops gracefully
until the supporting per-trade MFE column lands. Same safety scaffolding
as W1/W2.

The 3 boolean execution toggles (`use_atr_stops`, `use_trailing_stops`,
`use_limit_orders`) deliberately are NOT in W3 — they roll into W4
(weighted signal intensity) where they become 0.0/0.5/1.0 weights with
rotational A/B testing rather than binary on/off cliffs.

**Tests:** 5 new in `test_self_tuning_wave3.py`. Full suite: 775 passed.

**Tuner now manages 35 levers.** Layer 1 (parameter coverage) is now
substantively complete; remaining gaps are the 3 execution-toggle
booleans (deferred to W4) and the 2 placeholder rules awaiting feature
columns. W4 (weighted signal intensity) is next.

---

## 2026-04-25 — Autonomous tuning Wave 2: Group C (entry filters) — 8 new tunable parameters (Severity: medium, behavior)

**8 new entry-filter tuning rules** (all in `self_tuning.py`,
registered in `_apply_upward_optimizations` after the W1 set):

| Function | Parameter | Detection |
|----------|-----------|-----------|
| `_optimize_min_volume` | `min_volume` | Marginal-volume entries (≤1.5× threshold) WR < 30% → +50% |
| `_optimize_volume_surge_multiplier` | `volume_surge_multiplier` | Marginal surge entries WR < 35% → +0.25 |
| `_optimize_breakout_volume_threshold` | `breakout_volume_threshold` | Marginal breakout entries WR < 35% → +0.25 |
| `_optimize_gap_pct_threshold` | `gap_pct_threshold` | Marginal-gap entries (within 1.2×) WR < 35% → +0.5 |
| `_optimize_momentum_5d` | `momentum_5d_gain` | Marginal 5d-momentum entries WR < 35% → +0.5 |
| `_optimize_momentum_20d` | `momentum_20d_gain` | Marginal 20d-momentum entries WR < 35% → +0.5 |
| `_optimize_rsi_overbought` | `rsi_overbought` | Near-overbought entries (RSI ±5 of threshold) WR ≥55% → raise +2 |
| `_optimize_rsi_oversold` | `rsi_oversold` | Near-oversold entries WR ≥55% → lower -2 |

All read from `features_json` on resolved predictions via the new
shared helper `_bucket_by_feature(conn, feature_name)`. Rules
gracefully no-op when the relevant feature isn't logged yet (some
older predictions may not have full feature payloads). Same safety
scaffolding as W1: cooldown, reverse-if-worsened, bound clamping via
`param_bounds`, log to `tuning_history`.

**Tests:** 11 new in `test_self_tuning_wave2.py` covering each rule's
trigger logic, cooldown respect, no-op-on-missing-features, and
orchestrator registration. Full suite: 769 passed / 1 skipped.

**Tuner now manages 31 levers** (8 pre-existing + 10 W1 + 8 W2 + 5 wave-cross
[evaluation row, alpha_decay deprecation, 4 legacy strategy toggles
already counted as part of "8 pre-existing"]). Coverage of `trading_profiles`
columns is approaching 100%; W3 (Group B exits) closes the remaining
parameter rules.

---

## 2026-04-25 — Autonomous tuning Wave 1: Group A (concentration/risk) + Group D (timing) — 10 new tunable parameters (Severity: medium, behavior)

**Why this exists:** The whole point of QuantOpsAI is that it makes
better, faster, smarter tactical decisions than a person can. The
prior tuner managed only ~8 levers; the rest were either manually
configured or completely untouched. The full plan (see
`AUTONOMOUS_TUNING_PLAN.md`) brings every tactical parameter, signal,
regime context, and prompt structure under autonomous control across
9 layers, with cost discipline cross-cutting everything.

**Wave 1 ships the foundation** — Layer 1 Group A (concentration / risk)
and Group D (timing / flag) — plus the bounds-clamping infrastructure
that every later wave will use.

**New module: `param_bounds.py`.** Declarative `PARAM_BOUNDS` for every
tunable parameter — absolute min/max safety bounds. `clamp(name, value)`
helper. Tuning rules call `clamp` before writing so even a buggy
detection rule can't push a parameter to a dangerous value.

**10 new tuner functions** (all in `self_tuning.py`, registered in
`_apply_upward_optimizations`):

| Function | Parameter(s) | What it does |
|----------|--------------|--------------|
| `_optimize_max_total_positions` | `max_total_positions` | -1 on deep-loss + low-WR; +1 on strong-edge + healthy-winner |
| `_optimize_max_correlation` | `max_correlation` | Tighten 0.05 on weekly loss-cluster rate ≥40%; loosen on clean history + WR ≥55% |
| `_optimize_max_sector_positions` | `max_sector_positions` | -1 when overall WR < 35% |
| `_optimize_drawdown_thresholds` | `drawdown_pause_pct` | Tighten 0.02 in the WR drift zone (35–45%) |
| `_optimize_drawdown_reduce` | `drawdown_reduce_pct` | Tighten 0.01 in the WR drift zone |
| `_optimize_price_band` | `min_price`, `max_price` | Raise floor / lower ceiling when band-edge entries WR < 30%; capped at 0.5×–2.0× current to prevent identity drift |
| `_optimize_avoid_earnings_days` | `avoid_earnings_days` | Placeholder (no-op); activates when `days_to_earnings` is logged on each prediction |
| `_optimize_skip_first_minutes` | `skip_first_minutes` | Placeholder; activates when intraday entry-time is structured |
| `_optimize_maga_mode` | `maga_mode` | **Auto-disable** when predictions with political_context active WR ≥ 10pt below overall (≥20 samples) |

Every rule inherits the existing safety scaffolding: 3-day per-parameter
cooldown via `_get_recent_adjustment`, reverse-if-worsened guard via
`_was_adjustment_effective`, bound clamping, logging to `tuning_history`,
display via `display_name` namespaced fallback. Helper
`_safe_change_guarded` wraps the cooldown+history check.

**Documentation rewrite.** `SELF_TUNING.md` rewritten end-to-end —
removes the outdated "4 parameters" / "Future Parameters Planned Late
May 2026" sections and reflects the current 23 auto-tuned levers and
the 9-layer roadmap. `AI_ARCHITECTURE.md` Self-Learning section
expanded with the layered autonomy diagram and per-layer descriptions.

**Tests:** 23 new tests in `test_self_tuning_wave1.py` covering every
new rule (triggers correctly, respects bounds, respects cooldown, no-op
when conditions not met) plus an orchestrator-registration test.
`param_bounds.clamp` covered with under/over/in-range/unknown-param
cases. Full suite: 758 passed / 1 skipped.

**Next waves** (per `AUTONOMOUS_TUNING_PLAN.md`): W2 = entry filters,
W3 = exit parameters, W4 = weighted signal intensity (Layer 2), W5 =
per-regime overrides, W6 = per-time-of-day, W7 = cost guard, W8 =
per-symbol, W9 = cross-profile insight sharing, W10 = adaptive prompt
structure, W11 = self-commissioned strategies, W12 = capital
allocation, W13 = guardrail tests + Settings UI Autonomy section + final
doc pass.

---

## 2026-04-25 — Self-tuner: act on what it identifies (close 'recommendation only' hole) (Severity: medium, behavior)

**Problem:** When the tuner found a problem it knew the answer to, it
sometimes just emitted a "Recommendation:" string and called it done.
Concrete example flagged by user: "Insider Buying Cluster has 17% win
rate (3/18) vs 42% overall — consider removing from strategy mix" was
logged as 1 adjustment but no actual change was applied. The
underlying cause: only 4 of 16+ strategies had profile-level toggles,
so any modular strategy (insider_cluster, options-derived, etc.) the
tuner couldn't disable. The whole point of self-tuning is to act,
observe, and adjust — not to draft suggestions for a human.

**Fix — three layers:**

1. **Logic.** In `self_tuning._optimize_strategy_toggles`, the
   no-toggle branch now calls `alpha_decay.deprecate_strategy()` to
   actually remove the strategy from the active mix. The existing
   alpha-decay restoration pipeline (rolling Sharpe recovery) handles
   un-deprecating automatically. Cooldown applies via a synthetic
   parameter key `deprecate:{strategy_type}`. Same 3-day rule and
   reverse-if-worsened protection as the rest of the tuner. The
   "Recommendation: DISABLE short selling" branch was promoted from
   text to an actual `update_trading_profile(enable_short_selling=0)`
   call when 10+ short trades have <20% win rate AND negative P&L —
   defensive auto-action only. The reverse case ("ENABLE shorts") is
   deliberately left as a recommendation because flipping a high-risk
   feature ON without human review is dangerous (uncapped downside,
   margin requirements).

2. **Visibility.** `_task_self_tune` notification now separates
   "applied" from "recommended" counts (e.g., "Self-Tuning: 2
   applied, 1 recommended"). Body breaks them into APPLIED /
   RECOMMENDATIONS sections so the user can scan at a glance.
   Deprecated-strategies UI in the Strategy tab gets a "Restore"
   button (POSTs to a new
   `/ai/profile/<id>/restore-strategy/<strategy_type>` endpoint) so
   manual override is one click. Tuning history rows for deprecations
   surface via the existing display_name namespaced fallback —
   "deprecate:insider_cluster" renders as "Deprecate — Insider Buying
   Cluster".

3. **Guardrail.** New test `test_no_recommendation_only.py` AST-walks
   `self_tuning.py`, finds every "Recommendation:" string literal,
   and fails unless it matches an entry on a small ALLOWED list with
   a written rationale. Currently allowed: "Recommendation: enable
   short selling" (asymmetric on purpose: defensive disables get
   auto-applied; high-risk enables require human review). New
   "Recommendation:"-only paths fail this test until the author
   either wires a real action or adds an allowlist entry with
   rationale.

**Tests:** 6 new tests across `test_self_tuning_deprecation.py` and
`test_no_recommendation_only.py`: deprecation auto-action, cooldown,
already-deprecated short-circuit, toggleable strategies still use the
toggle path, allowlist enforcement, allowlist staleness check. Full
suite green at 735 passed / 1 skipped.

---

## 2026-04-25 — AI Win-Rate Trend chart added to AI Intelligence > Brain tab (Severity: low, feature)

**Problem:** No way to see whether the AI's prediction accuracy is
trending up or down over time. The Brain tab showed only the
all-time cumulative win rate — useful as a headline number, but
it hides recent shifts.

**Fix:** Added two pieces:

1. `ai_tracker.compute_rolling_win_rate(db_paths, window_days=7,
   lookback_days=60)` — returns a daily series of `{date, win_rate, n}`
   where each point is the win rate over the trailing 7 days. Days
   with zero resolved predictions in their window are returned with
   `win_rate=None` so the chart breaks the line cleanly instead of
   interpolating a fake value.
2. `metrics.render_win_rate_svg(series)` — server-rendered SVG line
   chart, mirroring the existing `render_equity_curve_svg` /
   `render_rolling_sharpe_svg` pattern (no JS chart library
   dependency). Y-axis 0–100% with grid lines at 0/25/50/75/100, a
   dashed 50% coin-flip baseline, green line if the latest point ≥ 50%
   else red. Gaps in resolved-prediction coverage render as broken
   polyline segments.

Wired into `ai_dashboard()` in `views.py` and rendered in the Brain
tab of `templates/ai.html` immediately after the headline win-rate
metric (so the user sees the trend right next to the cumulative
number).

**Tests:** 11 new tests in `test_ai_win_rate_chart.py` cover empty /
all-none series, pure winning/losing windows, mixed outcomes,
neutral-outcome exclusion, multi-DB aggregation, gap segmentation,
color selection. Full suite still green at 729 passed / 1 skipped.

---

## 2026-04-25 — Admin user table: humanize Created and Last Login columns (Severity: low, UX)

**Problem:** The admin user list showed raw ISO date/time strings:
`2026-03-28` for Created and `2026-04-23T14:36` for Last Login. The
"T" separator and lack of any natural formatting made the table read
as machine output.

**Fix:** Added a `friendly_date` Jinja filter to `display_names.py`
that renders a date or timestamp string as `"Mar 28, 2026"`. Updated
`templates/admin.html` to pipe `created_at` through `friendly_date`
and `last_login_at` through the existing `friendly_time` filter
(which renders `"Apr 23, 10:36 AM ET"`).

**Tests:** Existing 718-test suite passes — `friendly_date` is a
small additive function with no callers other than the template.

---

## 2026-04-25 — Self-tuning UI/digest: humanize parameter names and format values as percentages (Severity: medium, UX)

**Problem:** Two related leaks of internal identifiers and raw numeric
values to the user:

1. The weekly digest email's "Self-Tuning Changes" table showed
   snake_case parameter names like `ai_confidence_threshold`,
   `max_position_pct`, `strategy_gap_and_go` directly.
2. The dashboard's Self-Tuning History table (and the same table in
   `ai_performance.html` / `ai_operations.html`) rendered raw fractional
   decimals like `0.07 → 0.0805` for percentage params, instead of the
   user-facing `7.0% → 8.05%`.

**Root cause:** `_render_tuning_changes` in `ai_weekly_summary.py` and
the JS in `templates/ai.html` / `templates/ai_operations.html` both
pulled `parameter_name`, `old_value`, `new_value` straight from the
sqlite columns. There was no central knowledge of which params are
percentages vs. booleans vs. integers, and `display_names.py` had no
entries for self-tuning parameter keys.

**Fix:**
- Extended `display_names.py` with self-tuning parameter labels
  (`ai_confidence_threshold` → "AI Confidence Threshold", etc.),
  strategy-toggle labels (`strategy_gap_and_go` → "Strategy: Gap &
  Go"), bare strategy_type entries (`gap_and_go` → "Gap & Go" for the
  decay table), `_PERCENTAGE_PARAMS` and `_BOOLEAN_PARAMS` frozensets,
  and a `format_param_value(name, value)` function that renders a
  param value in its natural form (percentage / Enabled-Disabled /
  int / 2-dp float).
- `views.py`: `_format_param_name` now delegates to `display_name`;
  added `_format_param_value` helper; `api_tuning_history` populates
  `old_value_label` / `new_value_label` on each row; the two dashboard
  views populating the inline table do the same.
- `ai_weekly_summary.py`: `_render_tuning_changes` now passes
  `display_name(pname)` and uses `format_param_value` for old/new;
  `_render_decay_changes` wraps `strategy_type` with `display_name`.
- `templates/ai.html` (line 1157), `templates/ai_operations.html`
  (line 189): JS prefers `r.old_value_label` / `r.new_value_label`.
- `templates/ai_performance.html` (line 459): server-rendered template
  uses `| display_name` filter and `h.old_value_label or h.old_value`.

**Why it wasn't caught:** Display-formatting logic was scattered across
the API layer, JS templates, and the digest renderer, with no shared
source of truth — each layer had a partial humanization that left the
self-tuning params and percentage values uncovered. Tests covered the
data shape (`test_weekly_digest.py` passes raw rows through) but not
the rendered string content.

**Tests:** Existing 719-test suite passes. The render path for the
digest is exercised by `test_weekly_digest.py::TestRender::*` — they
verified no crash with the new code path. Follow-up TODO: add a
focused string-content assertion that "max_position_pct" and "0.07"
never appear in the rendered HTML for a tuned profile.

---

## 2026-04-24 — Blacklist: move from pre-filter to execution gate so stocks can recover (Severity: high, architectural)

**Problem:** The auto-blacklist at `trade_pipeline.py:817-837` rejected
any symbol with `win_rate == 0 AND total >= 3` resolved predictions
directly in the pre-filter, BEFORE the AI ever saw the candidate. That
meant no new predictions were ever recorded on blacklisted symbols,
their 0% win rate stayed 0% forever, and the stock was permanently
excluded from trading with no path back.

User framing (correct): **the blacklist should block TRADING, not
EVALUATION.** If the AI keeps predicting and those predictions start
winning, the symbol should earn its way back into the tradable set
automatically.

**Root cause:** pre-filter conflates two concerns — "don't risk capital
on this" (valid) and "don't even let the AI think about this" (side
effect). The latter broke the feedback loop that would let a stock
recover.

**Fix:** two surgical changes to `trade_pipeline.run_trade_cycle`.

1. **Pre-filter:** removed the `AUTO_BLACKLISTED` skip entirely. Kept
   the `get_symbol_reputation()` lookup (used downstream by
   `_build_candidates_data` to surface `track_record` to the AI).
   Blacklisted symbols now flow through multi-strategy, ranking,
   ensemble (4 AI calls), batch_select (1 AI call), and **prediction
   recording** — Step 4's existing logic writes an `ai_predictions` row
   for every candidate the AI evaluates, regardless of outcome.
2. **New Step 4.95 "Blacklist gate"** — right after the crisis gate
   and before execution. Filters `ai_trades` by reputation: entries
   (BUY/SHORT) for symbols with `win_rate == 0 AND total >= 3` are
   dropped with a `BLACKLIST_BLOCKED` detail entry and an activity-log
   row ("AI wanted BUY X but 0/N win rate — prediction recorded for
   re-evaluation"). Exits (SELL/COVER) are never blocked — blocking
   them would trap positions.

**Why this works without manual intervention:**
- The AI keeps predicting on blacklisted symbols every cycle.
- Those predictions resolve against price over 10 days.
- `get_symbol_reputation()` recomputes win_rate on each cycle.
- The instant a blacklisted symbol's win_rate rises above 0%
  (e.g., 1 win in 4 predictions → 25%), it no longer matches the
  blacklist predicate → gate passes → execution resumes.
- No persistent blacklist flag, no manual un-blacklisting, no stale
  state.

**What does NOT change:**
- The AI prompt is NOT modified — no "blacklisted" flag is injected
  into `candidates_data`. The AI already sees `track_record` (e.g.
  "0W/3L (0% win rate)") via `_build_candidates_data`, so it has
  visibility into the poor history without us biasing its decision
  with a dedicated flag.
- Exits are never blocked (we always want to let positions close).
- Symbols with < 3 resolved predictions are never blacklisted
  (insufficient evidence).
- Cost impact is marginal (+1-3 extra candidates per cycle in the
  shortlist; most blacklisted symbols don't trigger strong strategy
  signals and get filtered out at the ranking step anyway).

**Dashboard surface:** `BLACKLIST_BLOCKED` entries appear in the
pipeline output's `details` list. Each includes the AI's intended
action, the symbol's win/loss record, and the reason. The activity
feed logs the same event for historical review.

**Test coverage:** 10 new tests in `tests/test_blacklist_at_execution.py`:

Source-pattern contracts:
- Pre-filter no longer skips with `AUTO_BLACKLISTED`
- Step 4.95 gate + `BLACKLIST_BLOCKED` marker both present
- Gate touches only BUY/SHORT, never SELL/COVER
- `ai_analyst` source has no `blacklist` references (no prompt bias)

Behavioral:
- Entry blocked when reputation is 0% WR on 3+ predictions
- SELL/COVER never blocked even when blacklisted
- Symbols below 3 predictions not blacklisted (insufficient data)
- Symbols with no reputation record pass through
- **Recovered symbols (win_rate > 0%) pass the gate** — proves the
  "earn your way back" mechanism
- Mixed portfolio filters correctly (good/blacklisted/fresh/exit)

Tests: 709 → 719 passing.

---

## 2026-04-24 — Weekly AI-work digest email (Severity: feature)

**What:** New weekly digest — one consolidated email across all active
trading profiles — summarizing the autonomous changes the AI made, why,
and their observed effect. Fires every Friday at market close
(16:00 ET, right after the 15:55 ET self-tune run so the week's last
tuning decisions are captured).

**Sections:**
- Week at a glance — total realized P&L, trades, resolved-prediction
  win rate, AI cost, count of autonomous changes
- Per-profile table — buys/sells, resolved (win rate), realized P&L,
  AI cost per profile
- Self-tuning changes — parameter, old → new, reason, outcome_after
  (improved/worsened/neutral) with win_rate_after
- Strategy deprecations & restorations (Phase 3 alpha decay)
- Auto-strategy lifecycle transitions (Phase 7)
- Crisis-state transitions (Phase 10)
- Trading narrative — top 5 winners + bottom 3 losers with AI reasoning
  and confidence, grouped by profile

**Idempotency:** file marker at `{master_db_dir}/.weekly_digest_sent.marker`
stores the last-send date. The task is called from the daily-snapshot
block (per-profile) — the marker ensures only the first profile hitting
the task on Friday actually sends; the other 9 no-op. On send failure
the marker is NOT written, so next cycle retries.

**Gates:**
- `weekday() == 4` (Friday)
- `hour >= 16` in ET (matches the snapshot-block fire time)
- `marker_date != today` (not already sent today)

All gates use `datetime.now(ET)` — server is UTC, explicit conversion
matches the rest of the scheduler's timing-sensitive code.

**Why not 17:00 ET (my first draft):** the snapshot block only fires
once per day, on the first scheduler tick after 15:55 ET. A 17:00 gate
would have skipped the snapshot's only call to the digest task, so the
email would never send. 16:00 ET aligns with the snapshot fire time.

**Files:**
- `ai_weekly_summary.py` (new, ~420 lines) — `build_weekly_summary`
  across master + per-profile DBs; `render_html` emits subject + full
  HTML using existing `notifications.py` helpers
  (`_wrap_html`, `_section`, `_table`, `_color_pnl`, etc.)
- `multi_scheduler.py` — new `_task_weekly_digest` + hook inside the
  daily snapshot block
- `tests/test_weekly_digest.py` (new) — 13 tests covering build,
  render, day/time gating, idempotency, and retry-on-failure

**Uses existing infrastructure:** Resend via `notifications.send_email`,
env-var-based recipient (`NOTIFICATION_EMAIL`), styling helpers shared
with trade/veto/daily-summary emails.

**Tests:** 696 → 709 passing.

---

## 2026-04-24 — Stop MAGA oversold scan from spamming yfinance for dead tickers (Severity: low, log hygiene)

**Problem:** Today's audit showed 175 "possibly delisted" errors in the
production log across 30 unique symbols (`AUY, AZUL, CEIX, CFLT, CPE,
DLOCAL, ERJ, GPS, HEAR, IAS, LILM, PARA, SQ, VTLE, X, ...`). Yesterday's
screener fix filtered these out of `screen_dynamic_universe.fallback_universe`,
but the errors kept appearing — because a different code path was still
hitting yfinance for them every scan cycle.

**Root cause:** `multi_scheduler.py:543` — the MAGA mode oversold scan
loops directly over the raw hardcoded `seg["universe"]` from
`segments.py` (containing the known-stale hand-curated list) and calls
`get_bars(sym, limit=30)` for every symbol. Dead tickers return empty
from Alpaca → fall through to yfinance → yfinance logs "possibly
delisted" to stderr.

**Not a cost issue:** `get_bars` with empty/short bars results in the
MAGA loop's `if bars is None or bars.empty or len(bars) < 15: continue`
skip — no AI calls triggered, no trading impact. Pure log noise.
**Is a readability issue:** 170+ error lines/day make
`journalctl -u quantopsai` unreadable and would mask real failures.

**Fix:** New shared helper `screener.get_active_alpaca_symbols(ctx)` —
returns the set of Alpaca-active, tradable US equity symbols (same
filter rules as `screen_dynamic_universe`: US exchange, tradable,
no warrant/preferred suffixes). 24h in-process cache. Fail-open: on
Alpaca failure returns last-known-good set; on first-call-with-failure
returns empty (caller's fallback kicks in).

MAGA oversold scan now intersects `seg["universe"]` with this active
set before the loop. When the active set is empty (Alpaca completely
unreachable + no cache), uses the raw universe (preserves prior
behavior).

**Why the helper vs inline filter:** other hand-curated-universe paths
may get this same treatment later (e.g. the bigger
`DYNAMIC_UNIVERSE_PLAN.md` refactor). Centralizing the filter rules
means a future audit fixes them all in one place.

**Test coverage:** 6 new tests.
- `TestActiveAlpacaSymbolsHelper` (5): returns filtered set, cache hit,
  stale-refresh, stale-on-failure, empty-on-cold-failure
- `TestMigrationContract.test_maga_scan_filters_universe_via_get_active_alpaca_symbols`
  — source-pattern contract guards the MAGA block against regression

Tests: 690 → 696 passing.

**Expected impact:** delisted-ticker error lines drop from ~170/day to
zero within one scan cycle after deploy (once 24h active-symbols cache
warms). No trading behavior change. No cost change.

---

## 2026-04-23 — Gate earnings_analyst when no candidate has earnings in 14d window (Severity: medium, cost)

**Problem:** Today's ensemble audit showed `earnings_analyst` outputs
~45 tokens per call on average, while the other three specialists
(pattern, sentiment, risk) output ~1000 tokens each. That 45-token
response is the specialist returning "ABSTAIN — no earnings data to
analyze" for shortlists where no candidate has near-term earnings.
We pay ~1800 input tokens per call for effectively zero signal.

Today's split: of the ensemble's ~$1.45 total spend, `earnings_analyst`
was ~$0.15 (~10%). Over 95% of its calls appear to be abstentions.

**Fix:** New `EARNINGS_ANALYST_WINDOW_DAYS = 14` constant in
`ensemble.py`. Before running specialists in `run_ensemble`, check if
ANY candidate in the batch has earnings within `0 <= days_until <= 14`
via the existing `earnings_calendar.check_earnings` (DB-cached,
shortlist symbols are warm). If none do, skip `earnings_analyst`
entirely that cycle. The other three specialists run normally.

**Fail-open semantics** — three defensive properties, covered by tests:
- If `earnings_calendar` can't be imported at all → specialist runs
  (tested: `test_import_failure_fails_open`)
- If `check_earnings` raises for every symbol → specialist skipped
  ONLY when we have no evidence of upcoming earnings anywhere, but
  other specialists always run regardless
- If at least one candidate has earnings in window → specialist runs
  on the full batch (not filtered)

**Not affected by this gate:**
- Crypto profiles — already exclude `earnings_analyst` via
  `APPLICABLE_SPECIALISTS_BY_MARKET` (regression test added)
- Pattern / risk / sentiment specialists — always run
- `batch_select`, `sec_diff`, `transcript_sentiment`, etc. — unaffected

**Expected savings:** ~$0.15/day steady state across all equity
profiles. Larger on days when no earnings are in the window across
any profile's shortlist.

**What this is NOT:**
- NOT disabling the ensemble or reducing signal. `earnings_analyst`
  still runs on every cycle where a candidate has earnings within 14
  days — which is exactly when its output is most actionable
  (pre-announcement risk, post-announcement drift setups).

**Test coverage:** 6 new tests in `TestEarningsAnalystCostGate`:
- Skipped when no candidate has earnings
- Runs when any single candidate has earnings in window
- Boundary: 13 days in (runs), 15 days out (skipped)
- Fails open on per-symbol check_earnings exceptions
- Fails open on module import failure
- Crypto market still excludes it (via the older gate, not the new one)

Also updated two existing tests (`test_equity_markets_run_all_four`,
`test_cost_scales_with_chunks_not_candidate_count`,
`test_single_chunk_when_few_candidates`) to mock `check_earnings` so
they remain deterministic under the new gate.

Tests: 684 → 690 passing.

---

## 2026-04-23 — SEC filing backfill cost spike: cap AI diff calls per cycle (Severity: high)

**Problem:** Post-restart this afternoon (18:41 UTC) the `sec_diff` AI call
volume exploded to 487 calls in ~1 hour — 15-19 calls/minute sustained,
driving per-profile spend up $0.63. Rate peaked at 192 calls in the
20:05-20:09 window. Trajectory:

```
20:00-20:04:  46  calls
20:05-20:09: 192  calls  (peak)
20:10-20:14: 160
20:15-20:19:  89
```

**Root cause (not a regression, but a bounded-work design gap):**

`_task_sec_filings` calls `monitor_symbol(sym, days_back=180)` for every
symbol in positions + shortlist, per profile, every scan cycle. The task
had been blocked all morning by the `'recent_transactions'` KeyError
crashes (fixed earlier today). Once crashes stopped at 15:41 UTC and the
scheduler restarted at 18:41, `_task_sec_filings` finally ran — and
discovered ~180 days of uncached filings across symbols like STRC (37
filings), BMNR (49), RIG (14). The cache works correctly (verified:
487 AI calls = 487 new rows in `sec_filings_history`, zero duplicates;
delta = 0 between AI calls and rows written). But nothing bounded the
first-encounter cost per symbol. Per-profile databases mean each
profile pays the backfill cost independently when it first encounters
a high-filing-volume ticker.

**Fix (two changes to `sec_filings.monitor_symbol`):**

1. **Cap AI diff calls per invocation** — new `max_filings_per_cycle=5`
   param. After 5 filings analyzed, break out of the loop and record
   `deferred_to_next_cycle`. Filings arrive newest-first from EDGAR, so
   the cap always processes the MOST RECENT uncached filings first;
   older ones roll in on subsequent cycles. No data is lost; cost is
   just spread across time.
2. **Reduce `days_back` default 180 → 90** — one full quarterly cycle
   is enough context for `analyze_filing_diff` baseline comparison
   (the diff is against the most-recent prior filing in our DB, not a
   year-old one from EDGAR). Shrinks the backfill universe roughly
   in half.

Updated `multi_scheduler._task_sec_filings` caller to pass the new
values explicitly.

**Expected impact:**
- First-encounter of a high-volume symbol: ~5 AI calls (was up to 50)
- Subsequent cycles: same symbol, ~0 AI calls (cache hit)
- Steady state across portfolios: same as before (no change when caches
  are already warm)
- Upper bound per-cycle per-profile: `watchlist_size × 5` AI calls max

**What this explicitly is NOT:**
- NOT a cache bug. The `sec_filings_history` idempotency via
  `accession_number` lookup works correctly.
- NOT related to the `alt_data_cache`-based transcript_sentiment fix
  earlier today (that one IS working — 320 calls/day → 16/day confirmed
  post-restart).

**Test coverage:** 3 new tests in `TestBackfillCap`:
- `test_monitor_symbol_caps_ai_calls_per_invocation` — 20 filings, cap=5,
  assert exactly 5 AI calls and 15 deferred
- `test_default_cap_is_applied` — no explicit kwarg, still capped
- `test_cached_filings_skipped_before_cap_counts` — pre-cached filings
  don't consume cap budget (3 new fillings all analyzed under cap)

**Follow-up for a future session:**
- Cross-profile SEC filing cache (one EDGAR fetch shared across profiles
  of same user). Today's per-profile DB means N profiles × same symbol =
  N backfill passes. Design would need a shared cache in the master
  `quantopsai.db`. Not urgent — the cap bounds the per-profile cost.

---

## 2026-04-23 — sync.sh silently skipping deploys for weeks (Severity: high)

**Problem:** `./sync.sh 67.205.155.63` has been reporting "No files changed.
Nothing to sync." even when local files clearly differed from the droplet.
Today's earlier deploy of the dead-ticker fix was silently skipped by
sync.sh — had to be rsynced manually to land in production. This is the
root cause of how the local repo was able to drift 60 commits ahead of
origin without anyone noticing: each `./sync.sh` call appeared to succeed,
so nothing screamed that deploys weren't happening.

**Root cause:** Line 44 used `grep '^>f'` to pick file-transfer lines out of
`rsync --itemize-changes` dry-run output. But rsync's itemize direction
flags are:
- `<` — file being *sent to remote* (outgoing)
- `>` — file being *received from remote* (incoming)

Since we're always pushing local → droplet, every outbound change is
prefixed `<f...`, not `>f...`. The grep never matched, the `CHANGED`
variable stayed empty, the `-z` guard said "nothing to sync" and the
script exited cleanly without running the actual rsync or restarting any
services.

**Fix:** Changed `grep '^>f'` → `grep '^<f'` on line 44. One character.

**Bonus hygiene:** While in the file, added two excludes that were leaking
non-production files into the droplet when the detector finally did fire
(e.g., during manual testing):
- `.claude/` — Claude Code internal session state (scheduled tasks, caches)
- `.sync_test_marker` — reserved for sync diagnostics

**Why it wasn't caught:** No test exercises `sync.sh` end-to-end (it's a
shell script that SSHes to production — not trivial to mock). The dry-run
output has ordering subtleties that are easy to misremember; this kind of
rsync flag reversal is a classic copy-paste-era bug.

**Verification:** After the fix, `./sync.sh 67.205.155.63` correctly
identifies "sync.sh" as the changed file and proceeds with the full rsync.
Service restart logic (web vs scheduler detection) already worked
correctly — the issue was purely the change-detection gate.

**Follow-up (queued):** Add a smoke test that stubs `rsync --dry-run` with
a synthetic itemize-output and asserts that sync.sh correctly parses
outbound transfers. Would have caught this the moment the script was
written.

---

## 2026-04-23 — Dead-ticker log spam: filter fallback universe against Alpaca active assets (Severity: medium)

**Problem:** Every scan cycle produced ~20-30 `ERROR $SYMBOL: possibly delisted`
yfinance errors for tickers like `SQ`, `PARA`, `X`, `CFLT`, `IAS`, `MAG`,
`AUY`, `LILM`, `DLOCAL`, `HEAR`, `VTLE`, `ERJ`, `AZUL`, `SWI`, `GPS`. Yahoo's
website still renders these tickers (cached marketing pages), but Yahoo's
`/v8/finance/chart/SYMBOL` API returns 404 — the tickers moved or are gone:
`SQ → XYZ` (Block rebrand), `PARA → PSKY` (Paramount/Skydance merger),
`GPS → GAP`, `X` (US Steel acquired), `CFLT` (Confluent taken private),
plus several acquisitions/bankruptcies. Production Alpaca `get_asset()` calls
on every flagged symbol return `NOT FOUND`, confirming the source of truth.

**Root cause:** `screener.py:592-594` in `screen_dynamic_universe()` had a
"# Always include the curated universe" line that unioned the hand-curated
`segments.py` universe into the dynamic Alpaca sample:

```python
if fallback_universe:
    sample = list(set(sample + list(fallback_universe)))
```

The parameter name was misleading — `fallback_universe` was used as a
*supplement* on every run, not only as a fallback. So even though dynamic
discovery pulled fresh symbols from Alpaca, the hand-curated dead tickers
were still forced into the sample every cycle and ended up in
`get_snapshots()` and the yfinance fallback path, generating the log spam.

**Fix:** Intersect the fallback list with Alpaca's active-asset set
(`equity_symbols`, already built just above) before merging. Dead tickers
get filtered out as Alpaca stops listing them — the fix is self-healing as
future renames/delistings happen.

**Why it wasn't caught:** Existing tests verified that fallback symbols
*could* appear in output (`test_screener_alpaca_failure_falls_back_to_yfinance`),
but no test asserted that *dead* fallback symbols get filtered. The leak was
invisible to the test suite because no test mocked Alpaca returning fewer
symbols than the fallback list contained.

**Test coverage:** new `test_fallback_universe_filters_dead_symbols` in
`test_alpaca_data_migration.py` asserts that `ZOMBIE1`, `ZOMBIE2` symbols
passed in `fallback_universe` never reach `get_snapshots()` when Alpaca's
asset list doesn't contain them. Alive fallback symbols (`ALIVE_A`, `ALIVE_B`)
must still be carried through.

**Scope:** Quick-win surgical patch. The broader refactor documented in
`DYNAMIC_UNIVERSE_PLAN.md` (move sector classification to cached yfinance
lookups, freeze hardcoded lists into `segments_historical.py` for backtests
only, introduce a feature flag) remains queued as a separate multi-session
effort.

---

## 2026-04-23 — Continued fixes: exit order conflicts, confidence bypass, cache persistence (Severity: high)

**Exit order conflict fix.** `check_exits` crashed with "cannot open a short sell while a long buy order is open" when a limit buy was pending for the same symbol. Now cancels all open orders for a symbol before submitting the exit order.

**Confidence threshold bypass removed.** BUY signals previously bypassed the confidence threshold entirely — a 46% confidence BUY executed even with threshold at 70. This undermined the self-tuner's data-driven adjustment. All trades now must meet the threshold regardless of signal type.

**Transcript sentiment cache persisted to SQLite.** Was using in-memory cache that cleared on every restart, causing 221 AI calls ($0.29) in one day. Now uses `alt_data_cache` SQLite table. All SEC filings caches (filing metadata, text, insider data) also moved to persistent SQLite — no redundant EDGAR fetches on restart.

**Per-profile scan status replaces global timers.** Each profile bar shows its own state: scan step when active, "Next: 8m" when idle, "Queued" (amber) when due but waiting its turn. Global countdown timer blocks removed.

**friendly_time handles space-separated timestamps.** `task_runs.started_at` format is `2026-04-23 14:41:37` (space, not T) which `friendly_time` didn't parse, showing just "Apr 23" with no time.

**Changelog enforcement test.** New test verifies CHANGELOG.md contains today's date when any .py file was modified. Prevents commits without documentation.

---

## 2026-04-23 — Critical scan crash fix, dashboard hardening, performance (Severity: critical)

**CRITICAL: Scan cycles crashing since congressional data disabled.** When the congressional trading source was removed from the aggregator, the AI prompt builder still referenced `congress['recent_transactions']` with direct dict access. Empty dict + `None != "neutral"` evaluated True → `KeyError` → every scan cycle crashed for 1.5+ hours. Zero buys all day, only trailing stop exits.

**Fix:** Replaced ALL direct dict access (`dict['key']`) with `.get('key', default)` across every alt data field in `_build_batch_prompt()`. New test `TestPromptBuildDoesNotCrash` verifies the prompt builds successfully with empty, partial, and missing alt data — would have caught this before deploy.

**Scan failure banner on dashboard.** Red alert shows when any profile has failed scans in the last hour. Queries `task_runs` table for `status='failed'`. Would have immediately surfaced today's outage. Timestamps use `friendly_time` filter (ET).

**Profile error banner on dashboard.** Red alert shows when any profile has API authentication errors. Caught Large Cap 1M unauthorized key (stale key in `alpaca_accounts` table after regeneration).

**Dashboard load time: 17.5s → 2.2s.** Parallelized profile loading with `ThreadPoolExecutor(max_workers=10)` + 30-second in-memory cache for account info and positions.

**Countdown timers use actual ET market hours.** Was checking if last scan was <30min ago (false at market open until first scan completed ~22min later). Now checks Mon-Fri 9:30-4:00 ET directly.

**Display name fixes:**
- Exit triggers: `trailing_stop` → "Trailing Stop" (was `Trailing_stop` via `.capitalize()`)
- Sector flows: `comm_services` → "Comm. Services" (JS sectorNames mapping added)
- Ticker: HOLD predictions labeled "(HOLD prediction)" to distinguish from actual trades

**Data source corrections:**
- Dark pool ATS: fixed to use FINRA POST API with `compareFilter` by symbol (was returning 12.8M aggregate rows)
- Congressional trading: disabled (QuiverQuant paywalled, Senate/House GitHub repos dead since 2020, Finnhub premium-only)
- Patent filing: disabled (USPTO `api.uspto.gov` returns 403 — PatentsView v1→v2 migration incomplete, `searchText` param doesn't filter by assignee)
- "What the AI Sees" section updated: 12 per-symbol sources, 8 market-wide sources, 3 unavailable with honest explanations

**Other fixes:**
- AI cost "today" uses ET trading day (was UTC, showing $0 after 7-8 PM ET)
- Worst Periods hidden when <7 days of data (was showing empty $0.00 rows)
- Large Cap 1M Alpaca key updated in `alpaca_accounts` table (was stale after regeneration)

**Tests:** 678 total passing. New: `TestPromptBuildDoesNotCrash`, exit trigger display name enforcement, JS snake_case detection, sector flow name coverage.

---

## 2026-04-22 — Wave 2: 7 more free data signals (15 total) (Severity: feature)

Added 7 more alternative data sources, bringing the total to 15. The AI now sees:
- **Insider timing vs earnings** — insiders buying before earnings = bullish
- **Sector momentum ranking** — risk-on vs risk-off rotation detection
- **Dark pool ATS volume** — institutional accumulation/distribution (FINRA)
- **Market-wide GEX aggregate** — pinning vs expansion regime from options data
- **Earnings surprise history** — serial beater/misser track record (yfinance)
- **Earnings call transcript sentiment** — management tone via SEC EDGAR 8-K (AI-analyzed, cost-gated)
- **USPTO patent filing velocity** — innovation pipeline acceleration (PatentsView API)

All integrated into AI prompt, features_payload for meta-model, display names. 673 tests passing.

---

## 2026-04-22 — No-guessing test suite (Severity: infrastructure)

Added `test_no_guessing.py` with 26 tests that enforce correctness of names, schemas, data structures, and function signatures. Every bug caused by guessing during this session would now fail these tests before deploy:

- SQL table names must exist in known schemas (catches `sec_alerts` → real name `sec_filings_history`)
- Template JS must use real API field names, with blacklist of known bad names (catches `d.cboe_skew.value` → real name `skew_value`)
- `render_template` must pass every variable the template references (catches blank sections)
- Function calls must match actual signatures (catches `get_allocation_summary(profile_id)` → real sig `(db_path, market_type)`)
- API return fields verified against template consumers
- Display names cover all meta-model features
- View data consistency between performance and AI dashboards

673 total tests passing.

---

## 2026-04-22 — Trades pagination, countdown fix, AI cost timezone fix (Severity: medium)

**Trades page server-side pagination**: 50 trades per page with prev/next navigation. Column sorting via URL params (`?sort=pnl&dir=desc&page=1`) so sorting and pagination work together across page loads. Replaced client-side JS sort.

**Countdown timers always visible**: Timer blocks were hidden entirely after market close (`{% if any_profile_active %}` gate). Now always displayed — shows "Market Closed" after hours instead of disappearing. JS checks `market_open` flag from `/api/scheduler-status` to prevent showing "Scanning..." when market is closed.

**AI cost "today" uses ET trading day**: `date('now')` in SQLite is UTC, which flips to the next calendar day at 7-8 PM ET. Costs recorded during the trading day showed as $0.00 after that. Now computes the ET date boundary so "today" means the current trading day until midnight ET.

**Empty sections hidden**: Strategy Validations and Evolving Strategy Library sections hidden when no data exists instead of showing confusing "no data yet" messages.

---

## 2026-04-22 — AI Intelligence separated into own top-level page (Severity: feature)

**Problem**: The Performance page had 14 AI-related sections crammed into one tab alongside 5 tabs of traditional metrics. This is an AI-first system — it deserved proper organization.

**Solution**: New `/ai` route with 4 tabs matching the Performance page's tab pattern:
- **Brain** — prediction accuracy, confidence calibration, learned patterns, meta-model
- **Strategy** — allocation, validations, alpha decay, evolving library
- **Awareness** — Market Intelligence (NEW), SEC alerts, crisis monitor, events, ensemble
- **Operations** — self-tuning status/history, AI cost tracking, "What the AI Sees"

Performance page slimmed from 1721 to 762 lines — now only traditional metrics (tabs 1-5). All 18 original AI sections verified present in the new template via line-by-line diff against the original. Data computation copied verbatim from `performance_dashboard()` — no paraphrasing, no guessed field names.

**New Market Intelligence panel** on Awareness tab shows yield curve status (FRED API), CBOE Skew, estimated sector ETF flows, and economic indicators (unemployment, CPI, consumer sentiment, initial claims). Requires free FRED API key (`FRED_API_KEY` in `.env`).

**Full system audit** verified all pages load (10/10), all APIs return valid JSON (7/7), all 13 non-displayed system components functional (prediction resolution, trade pipeline, AI prompt, alt data, crisis detector, upward optimizer, display names, dotenv, backups, earnings cache, ensemble chunk size, political cache).

---

## 2026-04-22 — 8 free alternative data sources added (Severity: feature)

Added 8 new data sources to give the AI richer context for trading decisions. All free, no API keys required.

**Per-symbol (added to `alternative_data.py`):**
1. **Congressional Trading** — QuiverQuant API: which members of Congress are buying/selling each stock
2. **FINRA Daily Short Volume** — daily short volume ratio per symbol, flags when >50% (elevated)
3. **Insider Cluster Detection** — flags when 3+ insiders buy the same stock within 90 days
4. **Analyst Estimate Revisions** — EPS/revenue estimate direction (up/down/flat) from yfinance

**Market-wide (new `macro_data.py`):**
5. **Treasury Yield Curve** — FRED API: 2y, 10y, 30y rates, spread, inversion detection
6. **ETF Sector Flow Estimates** — computed from existing Alpaca bar data for sector ETFs
7. **CBOE Skew Index** — yfinance `^SKEW`: measures institutional tail-risk hedging
8. **FRED Leading Economic Indicators** — unemployment, CPI YoY, consumer sentiment, initial claims

**Pipeline integration:** All per-symbol data flows into the AI prompt per-candidate. All macro data renders in the market context section. New features flattened into `features_json` for meta-model training (7 new numeric, 3 new categorical).

**Crisis detector:** Two new signals — CBOE Skew extreme (>150) and yield curve inversion (10y-2y < 0).

**Tests:** 22 new in `test_alternative_data_new.py`. 647 total passing.

---

## 2026-04-22 — Remove cross-profile suggestions (Severity: cleanup)

Removed the cross-profile suggestion logic from `apply_auto_adjustments()`. It recommended copying another profile's confidence threshold but never auto-applied it, generating noise like "raise to 25" (the default floor). The upward optimizer now handles this better by analyzing each profile's own confidence band data and making targeted, auto-reversible adjustments.

---

## 2026-04-22 — UI clarity, viewer accounts, server-side pagination (Severity: medium)

**Profit factor clarity**: Renamed to "Portfolio Profit Factor" (trades tab, dollars) vs "Prediction Accuracy" (AI tab, directional %). Added tooltips explaining the difference. The AI picks winners at 1.50 but portfolio is at 0.95 because losing trades had larger positions — the upward optimizer's position sizing adjustments target this gap.

**AI profit factor was always N/A**: The `ai_perf["profit_factor"]` was initialized to 0.0 but never computed. Fixed. Also fixed to exclude HOLD predictions — HOLD "losses" aren't real losses (AI said don't trade, price moved, no money lost).

**Viewer accounts**: New `role` column on users (`admin` / `viewer`). Viewers see all data (linked to an admin via `linked_to_user_id`) but cannot change settings — all form controls disabled, POST routes blocked by `@admin_required`. New accounts default to viewer. Guest account created.

**Server-side pagination**: Tuning Status, Tuning History, Learned Patterns, and SEC Alerts load via AJAX API endpoints (`/api/tuning-status`, `/api/tuning-history`, `/api/learned-patterns`, `/api/sec-alerts`) with `page`/`per_page` parameters. Performance page loads instantly.

**SEC alerts broken by pagination**: API endpoint queried nonexistent `sec_alerts` table instead of using `sec_filings.get_active_alerts()`. Fixed.

**Tuning history missing profiles**: Profiles with only cross-profile suggestions went through the `if adjustments:` branch and skipped the `tuning_history` log. Now logs an "evaluation" row for every profile that was evaluated, regardless of whether changes were made.

**Confidence threshold cascade**: Was raising 25→60→70 in one run. Fixed to check the tighter band first and pick the right level in one step.

**Display names**: Added 30+ feature name entries to `display_names.py` (RSI, ATR, ADX, etc). Fixed `_analyze_failure_patterns` to use `display_name()`. Added test enforcing every meta-model feature has a display name entry.

**Activity ticker profile names**: Activity feed entries now show `[Profile Name]` so you can tell which account generated the activity.

**Stalled task diagnostics**: Watchdog now diagnoses probable cause (service restart, slow API, hung fetch) instead of generic "investigate in journalctl."

**Smart deploy script**: `sync.sh` now auto-detects changed files, only restarts affected services, waits for cycle boundaries before restarting the scheduler.

**Daily backups**: Cron job at 1 AM ET, 14-day retention, uses `sqlite3 .backup` for consistency.

**Earnings calendar**: Refresh interval 24h→7d. Smart cache: if a future earnings date is stored, no refetch until that date passes.

---

## 2026-04-22 — Self-tuner upward optimization (Severity: feature)

**Problem**: The self-tuner only prevented disasters (win rate < 35%) but never tried to improve a profile already performing at 50-60%. A profile at 61% win rate got "no changes needed" when it should be pursuing 70%+.

**Solution**: Added 5 upward optimization strategies to `apply_auto_adjustments()` in `self_tuning.py`, gated on `overall_wr >= 35%`:

1. **Confidence threshold optimization** — finds the best-performing confidence band and raises the threshold one band at a time
2. **Regime-aware position sizing** — reduces exposure in losing market regimes, increases in winning ones
3. **Strategy toggle optimization** — disables worst-performing strategies (never the last one)
4. **Stop-loss/take-profit optimization** — widens stops that trigger too early, tightens TPs that never hit
5. **Position size increase** — increases position size when edge is proven (55%+ WR, 30+ samples, cap 15%)

**Safety**: One change per run (for clean auto-reversal attribution), 3-day cooldown, history check prevents repeating failed adjustments, hard caps on all parameters.

**Also fixed**: Confidence threshold cascade bug — was raising 25→60→70 in one run instead of picking the right level once. Deploy script now auto-detects changed files and only restarts affected services, waits for cycle boundaries before restarting scheduler.

**Tests**: 13 new in `test_self_tuning_upward.py`. 625 total passing.

---

## 2026-04-22 — Complete yfinance→Alpaca migration for all equity data paths (Severity: high)

**Problem**: Multiple modules were still using yfinance (`yf.download`, `yf.Ticker`) for equity price data instead of the paid Alpaca API. This caused Yahoo rate limit errors (`YFRateLimitError: Too Many Requests`), thread-safety crashes, and silent data failures. The screener batch downloads were the worst offenders — hitting Yahoo with 50+ symbols simultaneously and getting rate-limited.

**Files migrated to Alpaca primary**:
- `screener.py`: `screen_by_price_range`, `find_volume_surges`, `find_momentum_stocks`, `find_breakouts` — all now use `_get_bars_for_symbols()` via Alpaca
- `market_data.py`: `get_sector_rotation` (sector ETFs), `get_relative_strength_vs_sector`, `get_snapshot`, `get_bars_daterange` — all now try Alpaca first
- `correlation.py`: `_fetch_returns` — now uses `get_bars` per symbol via Alpaca
- `metrics.py`: `_fetch_benchmark_returns` — now uses `get_bars_daterange` via Alpaca
- `backtester.py`: `_download_symbol`, `_fetch_universe_batch` — both now use Alpaca
- `ai_tracker.py`: `_get_current_price` — now uses `api.get_latest_trade()` directly
- `app.py`: added `load_dotenv()` — gunicorn web process had no env vars, causing all Alpaca calls from the dashboard to fail silently (broke sector rotation widget)

**Earnings calendar optimization**: Changed refresh interval from 24 hours to 7 days, and added smart cache: if a future earnings date is stored, no refetch needed until that date passes. Earnings are quarterly events — daily re-checking was pointless and hammered Yahoo.

**Ensemble cost optimization**: Raised `CHUNK_SIZE` from 5 to 15 in `ensemble.py`. Each specialist now processes the full shortlist in 1 API call instead of 3. Cuts ensemble AI cost ~60%.

**Political context cache**: Added 30-minute cache in `trade_pipeline.py` so all MAGA-mode profiles share one political analysis call instead of each making their own.

**Tests added**: 6 new tests in `test_alpaca_data_migration.py` enforcing Alpaca-first in screener, ai_tracker, correlation, metrics, market_data, backtester, and both app.py/multi_scheduler.py dotenv loading. 610 total tests passing.

---

## 2026-04-22 — AI prediction resolution broken for all profiles (Severity: critical)

**Problem**: Dashboard showed "0 / 20 (0%)" for Large Cap resolved predictions despite having trades going back 5 days. Small Cap Aggressive had only 5 resolved out of 380 total. Multiple profiles were silently failing to resolve predictions every cycle.

**Root causes (three cascading failures)**:

1. **`days_held` column missing** — The `ai_predictions` table in several profile DBs lacked the `days_held` column. The resolution `UPDATE` statement included `days_held = ?` which threw `sqlite3.OperationalError: no such column: days_held`, killing the entire resolution task. Fixed in the earlier `_migrate_all_columns` patch, but that fix wasn't deployed to all profile DBs until this session.

2. **Alpaca data API returning 401 in scheduler** — `multi_scheduler.py` never imported `config.py` or called `load_dotenv()`. Environment variables `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` were not loaded in the scheduler process. The shared `market_data._get_alpaca_data_client()` got empty keys → 401 Unauthorized → fell back to yfinance. yfinance then failed intermittently due to thread-safety issues in the ThreadPoolExecutor, causing 0 prices → 0 resolutions.

3. **`_get_current_price` ignored the per-profile API client** — The function called `market_data.get_bars(symbol, api=api)` but `get_bars` ignores the `api` parameter entirely and uses its own module-level client. The per-profile API client (which has valid, authenticated credentials) was passed but never used.

**Fix**:
- Added `from dotenv import load_dotenv; load_dotenv()` at top of `multi_scheduler.py` before any imports that read env vars
- Rewrote `ai_tracker._get_current_price()` to use `api.get_latest_trade(symbol)` as primary path (uses the per-profile authenticated API directly), falling back to `market_data.get_bars()` only if that fails
- Added price validation guard in `record_prediction()`: rejects predictions with `price_at_prediction <= 0` to prevent unresolvable records
- Fixed 40 existing predictions with `price=0` (all from Apr 17 profile setup day) by marking them `status='resolved', actual_outcome='data_error'`
- Added thread-safety locks to `political_sentiment.py` and `options_oracle.py` yfinance calls

**After fix**: Manual resolution run resolved 124 predictions for Small Cap Aggressive (was stuck at 5), 79 for Small Cap Shorts, 42 for Small Cap, 35 for Mid Cap. All profiles now resolving correctly.

**Why it wasn't caught**: The resolution task swallowed the `OperationalError` inside the task runner's generic try/except, logging `[TASK FAIL]` but continuing. The subsequent price-fetch failures returned None silently (no warning logged because `get_bars` returns empty DataFrames, not exceptions). The dashboard showed "0 resolved" which looked like "no data yet" rather than "resolution is broken."

**Test coverage**: Existing 605 tests pass. The `_get_current_price` change is covered by the prediction resolution integration test which mocks the API client. The `record_prediction` price guard prevents future price=0 records.

---

## 2026-04-22 — yfinance thread safety audit (Severity: medium)

**Problem**: Thread-safety wrappers (`yf_lock`) were missing on `yf.Ticker()` calls in `political_sentiment.py` and `options_oracle.py`. These could cause `RuntimeError: dictionary changed size during iteration` when multiple profiles run concurrently in the ThreadPoolExecutor.

**Fix**: Wrapped yfinance Ticker creation in both modules with `yf_lock._lock`. No functional change — purely thread safety.

**Honest assessment of remaining yfinance usage**: yfinance is correctly used as the ONLY source for: VIX index data, fundamentals, insider trades, options chains, earnings dates, analyst recommendations. These have no Alpaca equivalent. For equity price data (bars, latest trade), Alpaca is now the primary source everywhere. `backtester.py` still uses yfinance directly for bulk historical data (intentional — 720-day cache per symbol for backtesting).

---

## 2026-04-15 — FIFO P&L backfilled onto closed BUY rows (no more useless "open" or "closed" labels)

**Severity:** medium UX — user feedback: "having a bunch of random closed
is just as useless as a bunch of random opens. You know what you bought
it for and what you sold it for so you can calculate the P&L."

**Root cause of the useless display:** the trades table design puts pnl
only on SELL (exit) rows. BUY (entry) rows had `pnl=NULL` — even after
`reconcile_trade_statuses` marked them `status='closed'`, the trades
page had no dollar value to display, so it fell back to "closed" or
"open" labels that told the user nothing.

**Fix (proper one):** extend `reconcile_trade_statuses` with FIFO lot
matching. For each symbol, walk trades in timestamp order; every BUY
opens a lot; every SELL consumes qty from the oldest open lots and
accumulates realized P&L back onto each BUY row's `pnl` column.

The algorithm handles:
- Simple round-trips (1 BUY → 1 SELL)
- Partial exits (1 BUY → 2+ partial SELLs) — sums attributed P&L
- Multiple round-trips (BUY → SELL → BUY → SELL) — each entry row
  gets its own correct P&L
- Open positions (BUY without matching SELL) — left with `pnl=NULL`
- Already-set `pnl` — never overwritten

**Template now:** the trades-table macro shows realized P&L on every
closed row (BUY or SELL), unrealized P&L on held positions, "open"
only for truly uncalibrated rows (new BUY with no live quote yet).
No more "closed" or "exit" labels — every closed trade shows a dollar
number.

**Backfill on production:** Mid Cap had 4 closed BUY rows with NULL
pnl — after the one-shot reconcile run, all 4 carry their realized
P&L (e.g., HIMS 04-13 BUY now shows +$15.20 across the two partial
sells on 04-15). Small Cap and Large Cap had no closed positions
yet (their exits happen on Mid Cap primarily).

**Tests** (`test_trade_status_reconcile.py` — 16 total, +6 new):
- Simple round-trip: BUY 10@$100, SELL 10@$110 → BUY pnl +$100
- Losses record correctly as negative
- Partial sells sum to total realized P&L on the BUY row
- Multiple round-trips: each BUY gets its own lot's P&L, not merged
- Still-open BUYs stay `pnl=NULL`
- Existing `pnl` values never overwritten

**Test count:** 550 (was 545 + 6 new − 1 replaced obsolete).

---

## 2026-04-15 — Self-tuning now visible even when it doesn't change anything

**Severity:** UX — user was certain self-tuning wasn't running because
nothing ever appeared in the dashboard.

**Reality check:** Self-Tune runs daily (alongside the Daily Snapshot
task), but it needs ≥20 resolved AI predictions per profile before it
will adjust anything. Current state: Mid Cap 8 resolved, Small Cap 0,
Large Cap 0. The tuner is alive but patiently waiting for data.

**The UX gap:** when `apply_auto_adjustments()` returned an empty list
(no changes), the scheduler silently exited with a log line nobody
reads. No activity row. No dashboard signal. User saw nothing →
assumed the tuner was broken.

**Fix:**
- New `self_tuning.describe_tuning_state(ctx)` returns a struct with
  `can_tune`, `resolved` (current count), `required` (20), and a
  human-readable `message` explaining the current state.
- `_task_self_tune` in the scheduler now logs an activity entry
  EVERY run — whether it changed parameters, found no adjustments
  needed, or is waiting for more data. The title + detail distinguish
  the three cases so the user can confirm the tuner is alive at a
  glance.
- New "Self-Tuning Status" panel on Performance > AI Intelligence
  tab shows per-profile: resolved-predictions progress bar
  (e.g. "8 / 20 (40%)"), current status ("Collecting data" vs
  "Active"), last-run timestamp, and the human-readable message.
  Hides only if there's literally no data at all.

**Tests** (`test_self_tuning_visibility.py` — 5 tests):
- `can_tune=False` when self-tuning disabled on profile
- Resolved count reads from ai_predictions table
- `can_tune=True` when resolved ≥ 20
- Missing `ai_predictions` table → safe message, no crash
- Message copy communicates "waiting for data" (not failure)

---

## 2026-04-15 — Trade statuses reconciled (BUY rows no longer show "open" after exit)

**Severity:** medium UX — the Trades page was showing closed positions
as "open" forever.

**Symptom:** HIMS BUY on 04-13 (qty 20) was fully exited by two SELLs on
04-15 (qty 5 + 15). The BUY row stayed `status=open, pnl=null` and
displayed as "open" on the trades page. Most exit-SELL rows also
carried `status=open` despite having realized `pnl`.

**Root causes:**
1. `trader.check_exits` logged exit SELLs without passing `status="closed"`
   (unlike `trade_pipeline.py` which did). Default was "open".
2. Nothing ever went back and marked matching BUY rows as closed when
   their positions flattened.

**Fixes:**
- `trader.py` now passes `status="closed" if pnl is not None else "open"`
  on exit SELLs, matching the pipeline's behavior.
- After an exit, both `trader.py` and `trade_pipeline.py` run an
  inline `UPDATE trades SET status='closed' WHERE symbol=? AND
  side='buy' AND status='open'` — flattens entry rows the moment the
  position closes.
- New `journal.reconcile_trade_statuses(db_path, open_symbols)` —
  authoritative reconciliation using live Alpaca positions as ground
  truth. Fixes any drift (old rows from before this fix) by marking
  open BUYs closed when their symbol isn't currently held.
- New scheduled task `_task_reconcile_trade_statuses` runs every
  exit cycle (5 min) to catch any drift automatically.
- One-shot backfill run against the live DBs: Mid Cap fixed 5 sells
  and 4 buys; Small/Large had no drift.

**Tests** (`test_trade_status_reconcile.py` — 10 tests):
- SELL rows with pnl but open status → closed
- SELL rows without pnl → left alone (can't confirm)
- Already-closed SELLs unchanged
- BUY rows for symbols not in live positions → closed
- Empty open_symbols (no positions) → all open BUYs closed
- BUY rows for still-held symbols → preserved
- Heuristic path (no positions list): BUY with matching SELL → closed
- Count-reporting correctness

**Test count:** 543 (was 528 + 15 new across two features).

---

## 2026-04-22 — Universal schema migration + cost tracking fix

**"Resolve AI Predictions" failing every cycle on 3 profiles:**
`sqlite3.OperationalError: no such column: days_held` — profiles 4, 5,
and 9 were created before the `days_held` column was added to the
`ai_predictions` schema. The old per-column migration functions
(`_migrate_slippage_columns`, `_migrate_prediction_columns`) only
covered specific columns and missed `days_held`.

**Fix:** Replaced the per-column migrations with `_migrate_all_columns()`
— a single function that defines every expected column for every table
and adds any that are missing via ALTER TABLE. Runs on every `init_db()`
call. Safe to run repeatedly. Will catch any future schema additions
automatically.

**AI cost "today" was showing last 24 hours, not calendar day:**
`spend_summary()` used `datetime('now', '-1 day')` which is a rolling
24-hour window. Changed to `date('now')` for the "today" bucket so
it matches the Anthropic billing console. Added total cost row to
dashboard overview table.

---

## 2026-04-21 — Max positions cap removed (10 → 100)

All profiles were maxed at 10/10 positions by mid-morning, blocking
all new trades for the rest of the day. The arbitrary cap was
redundant — position sizing (10% max per position), correlation
limits (0.7), and sector caps (5) already control concentration
risk based on actual portfolio characteristics, not an arbitrary
count. Set to 100 (effectively uncapped) to maximize data collection.

---

## 2026-04-21 — Trades page: single P&L column, brokerage-standard layout

Replaced the two-column Unrealized/Realized layout with a single P&L
column. BUY rows show entry info only (no P&L). SELL rows show
realized P&L. Dashboard shows unrealized on open positions. Matches
Schwab/Fidelity trade history view. Removed trades page enrichment
that was adding unrealized to BUY rows.

---

## 2026-04-21 — Archived profiles hidden from all UI pages

Disabled profiles (e.g. "Crypto (archived)") no longer appear in
dashboard tabs, trades dropdown, performance dropdown, or AI
performance dropdown. Settings page has a "Show archived profiles"
checkbox that reveals them dimmed when needed.

---

## 2026-04-21 — Split P&L into Unrealized + Realized columns

**Problem:** BUY and SELL rows both showed the same realized P&L,
making it look like double the profit or loss on every trade.

**Fix:** Replaced the single "P&L" column with two:
- **Unrealized** — live P&L on positions still held (BUY rows with
  open positions). Blank once the position closes.
- **Realized** — locked-in P&L from closed positions (SELL rows only).
  Blank while position is still open.

Every dollar amount appears exactly once. No double-counting.

Removed the FIFO backfill that wrote pnl onto BUY rows. Cleared
existing backfilled values from all profile databases.

---

## 2026-04-21 — Prediction resolution too slow for self-tuning to activate

**Problem:** 82 actual trades across 10 profiles, but self-tuning
hadn't activated on any profile. Self-tuning requires 20 resolved
predictions, but most profiles had 0-7 resolved despite hundreds of
pending predictions.

**Root cause:** Resolution thresholds were too strict. BUY predictions
needed a +5% price move to count as "win" — most stocks don't move 5%
in a few days. Meanwhile the system's actual stop-loss is 3% and
take-profit is 10%, so the resolution criteria didn't match the
trading parameters.

**Fix:** Lowered thresholds to match actual trading behavior:
- BUY/SELL win/loss: 5%/3% → 2%/2%
- HOLD resolve: 5 days → 3 days
- Timeout: 20 days → 10 days

**UI:** Added explanation on the AI Performance tab explaining the
difference between resolved predictions (AI forecasting accuracy
across all candidates) and closed trades (actual executed trades
with real P&L). Tooltips on each metric card.

---

## 2026-04-20 — Market regime broken all day + silent failure test suite

**Market regime bug:** When I migrated SPY data from yfinance to Alpaca,
I left `spy_hist["High"]` / `["Low"]` / `["Close"]` in title case.
Alpaca returns lowercase. Result: "Failed to detect market regime: 'High'"
174 times today. **Every trade decision today was made without knowing
if the market was bullish, bearish, or sideways.** Fixed to lowercase.

**Silent failure test suite** (`test_silent_failures.py` — 11 tests):
Catches the exact class of bugs that keep recurring — column case
mismatches, Alpaca vs yfinance format differences, missing thread
locks, API calls to services we don't subscribe to. These tests
would have caught the market regime bug before deploy.

**ETF filter expanded:** Added JPST, RSP, SRTY, SOXS, LABU, LABD.

**Test count:** 607 (was 596 + 11).

---

## 2026-04-20 — Fix ensemble sharing race condition + disable intraday emails

**Ensemble race condition:** Parallel profiles of the same market type
were both missing the ensemble cache simultaneously and running
duplicate AI calls. Added a threading lock to `_get_shared_ensemble()`
so only one thread runs the ensemble per market type — the others
wait and reuse the cached result. Mid Cap had 60 ensemble calls today
when it should have had ~12.

**Email reduction:** Disabled `notify_trade`, `notify_exit`, and
`notify_veto` — all visible on the dashboard. Only EOD summary,
self-tuning adjustments, and system errors are emailed now. Prevents
hitting the Resend daily limit with 10 profiles.

---

## 2026-04-17 — Eliminate yfinance rate limiting: DB caching, Alpaca for SPY, ETF filter

**Problem:** ~500+ yfinance errors per day from rate limiting.
Alternative data (insider, fundamentals, short interest) was fetched
per-symbol per-cycle from yfinance with only an in-memory cache that
reset on every deploy. Market regime used yfinance for SPY. ETFs like
SOXL and AMZD were in the screener universe but have no fundamentals,
flooding "no data found" errors.

**Fixes:**
1. **Alternative data DB cache** — `alt_data_cache` SQLite table replaces
   in-memory cache. Survives restarts. Each symbol fetched once per TTL
   (24h for insider/fundamentals, 1h for short interest). Thread-locked
   yfinance calls prevent race conditions.
2. **Market regime uses Alpaca for SPY** — `get_bars("SPY")` instead of
   `yf.Ticker("SPY")`. VIX stays on yfinance (Alpaca doesn't serve
   index data) but is thread-locked.
3. **ETF filter** — 40+ known ETFs/leveraged products (SOXL, TQQQ, SPY,
   QQQ, AMZD, NVDL, etc.) excluded from the screener universe. They
   don't have fundamentals data and aren't tradeable candidates.

**Expected impact:** yfinance calls drop from ~3,000/day to ~300/day.
Rate limiting errors should be near zero.

**Tests** (`test_data_fixes_apr17.py` — 8 tests):
- Alt data cache: persists to SQLite, respects TTL, survives reload
- ETF blocklist contains key symbols
- Market regime uses Alpaca get_bars, not yf.Ticker for SPY
- Metrics capital: per-profile forward-fill, no double-multiply
- Annualized return: no overflow on <7 days

**Test count:** 596 (was 588 + 8).

**"What the AI Sees" section updated** to match actual code: added
Strategy Votes, Last Prediction memory, Portfolio State, Market Regime.
Moved to collapsible reference at bottom of AI Performance tab. Tab
renamed from "AI Intelligence" to "AI Performance."

---

## 2026-04-17 — System hardening: cost alerting, cross-account reconciliation, metrics fixes

**Fixes:**
- **Metrics initial_capital bug** — `calculate_all_metrics` was doubling
  the total capital (passed $2.15M total, then multiplied by num_profiles
  again). Showed +1279%, then -56%, then +33% at various stages. Now
  correctly shows -0.1%. Per-profile capital map passed for accurate
  snapshot forward-fill.
- **Legacy DB inclusion** — old segment DBs (quantopsai_midcap.db etc.)
  were being included in the metrics aggregation despite being empty,
  inflating the profile count.
- **Disabled profiles included** — Profile 2 (disabled crypto) was counted
  in DB paths and capital calculations.
- **Annualized return overflow** — `(1+return)^(365/1)` crashed with
  OverflowError on day 1. Now requires 7+ days before computing.
- **Recovered trades backfilled** — 21 manually recovered trades now have
  the original AI reasoning and confidence from their matching predictions.
- **Auto-exit label** — exit trades (trailing stop, SL, TP) show "Auto-exit"
  instead of "--" in the AI Confidence column.
- **Admin page** — reads from actual per-profile cost ledger instead of the
  dead `user_api_usage` table.

**New features:**
- **API cost alerting** — daily spend check runs with the snapshot. Alerts
  in the activity feed when total exceeds $3/day.
- **Cross-account reconciliation** — wired into scheduler. Runs once per
  Alpaca account per snapshot cycle. Compares sum of virtual positions
  against Alpaca's actual holdings, logs drift warnings.
- **Cost per profile on dashboard** — overview table shows each profile's
  AI cost today.

---

## 2026-04-17 — Specialist ensemble + SEC filings shared across profiles ($5.75 → ~$2/day)

**Severity:** high — API costs were 3× the estimate

**Problem:** Each of the 10 profiles ran its own specialist ensemble
(4 AIs × 3 chunks = 12 calls) independently, even when profiles
of the same market type evaluated the exact same candidates. Mid Cap,
Mid Cap 25K, and Mid Cap 500K all asked the same 4 specialists the
same questions about the same stocks — just with different capital.
Same issue with SEC filing diffs: 612 AI calls/day instead of ~20.

**Why sharing makes sense:** The specialist ensemble evaluates the
CANDIDATES, not the profile. An earnings analyst's verdict on AAPL
doesn't change because one profile has $25K and another has $500K.
The candidates are identical (same screener, same market type), so
the verdicts are identical. Only the final batch trade selector
needs to be per-profile because it makes sizing decisions based on
each profile's capital, positions, and risk parameters.

**Fix:**
- `_get_shared_ensemble()` in `trade_pipeline.py` caches ensemble
  results per market_type per 15-minute cycle. First profile to
  shortlist runs the ensemble; subsequent profiles of the same
  market type reuse the cached verdicts.
- SEC filing monitor (`_task_sec_filings`) now runs once per
  market_type per cycle instead of per-profile. Same filings,
  same AI diffs — no reason to repeat.

**Cost impact:**
| Call type | Before | After | Savings |
|---|---|---|---|
| Specialist ensemble | 1,437 calls ($4.20) | ~430 calls ($1.26) | 70% |
| SEC filing diffs | 612 calls ($0.69) | ~60 calls ($0.07) | 90% |
| Batch selector | 119 ($0.76) | 119 ($0.76) | 0% (correct) |
| Political context | 18 ($0.09) | 18 ($0.09) | 0% (already cached) |
| **Total** | **$5.75/day** | **~$2.10/day** | **63%** |

**What stays per-profile (correctly):**
- Batch trade selector — different capital = different sizing
- Position sizing / risk checks — profile-specific
- Order execution — routed to profile's Alpaca account
- Trade logging — per-profile database

---

## 2026-04-17 — Small return percentage now shows 2 decimal places

When Total Return rounds to 0.0% but P&L is non-zero (e.g. $791 on
$2.15M combined capital = 0.04%), the display now shows +0.04% instead
of the misleading +0.0%.

---

## 2026-04-17 — Dashboard tabbed UI for 10 profiles

Replaced the vertically-stacked profile list with a tabbed layout:
Overview tab (summary table of all profiles + activity + sectors) plus
one tab per profile. Eliminates the massive scroll on the dashboard.

---

## 2026-04-17 — Parallel profile execution + droplet upgrade

Upgraded DigitalOcean droplet from 1 CPU/1GB ($6) to 2 CPU/2GB ($18).
Added ThreadPoolExecutor(max_workers=3) to run all profiles in parallel.
Total cycle time dropped from ~15 min (sequential) to ~5-8 min.

---

## 2026-04-17 — Order guard prevents after-hours trades

New `order_guard.py` checks `ctx.is_within_schedule()` at order
submission time, not just at cycle start. Prevents accidental
after-hours fills when pipeline takes longer than the schedule window.
10 tests covering market_hours, extended_hours, 24/7, weekends.

---

## 2026-04-17 — Sortable trade columns + ET timestamps + consistent P&L format

Trades page: clickable column headers sort by any field. Timestamps
converted from UTC to Eastern Time with "ET" label. All P&L entries
show both dollar and percentage consistently. Friendly time filter
added to display_names.py.

---

## 2026-04-17 — Screener results shared across same-market-type profiles

**Severity:** optimization — reduces API costs ~70% on screener/data calls

**Problem:** 10 profiles were each running their own screener
independently. Mid Cap, Mid Cap 25K, and Mid Cap 500K all screened the
same "midcap" universe — 3× the Alpaca snapshot calls, 3× the MAGA
oversold scan, 3× the alternative data lookups (insider trades, short
interest, options chains).

**Fix:** `_get_shared_candidates()` caches screener + MAGA results per
market_type per 15-minute cycle. First profile to run screens the
universe; subsequent profiles with the same market_type reuse the
cached result. Logs "Using shared screener results for midcap" so
it's visible.

**Savings:** 10 screener runs → 3 (one per market type). Each screener
run includes ~100 symbol-level data fetches. Net: ~700 fewer API calls
per cycle.

**AI calls unchanged:** Each profile still runs its own specialist
ensemble + batch selector because they have different capital, positions,
and risk parameters. That's correct — a $25K profile should make
different sizing decisions than a $500K profile on the same candidates.

---

## 2026-04-17 — Earnings calendar moved to DB cache (eliminates yfinance error floods)

**Severity:** high — yfinance earnings checks were flooding 401 errors

**Root cause:** Every scan cycle checked each candidate's earnings date
by calling `yf.Ticker(symbol).calendar` individually. With 10 profiles
× 30 candidates = 300 yfinance calls per cycle, Yahoo rate-limited
and returned "Invalid Crumb" 401 errors. The earnings filter silently
failed, allowing trades into earnings announcements.

**Fix:** Rewrote `earnings_calendar.py` to store dates in SQLite
(`earnings_dates` table in main DB). yfinance is called only once per
24 hours per symbol. All subsequent checks read from DB — instant,
zero API calls, zero errors. 300 yfinance calls/cycle → 0.

---

## 2026-04-17 — Position values visible, scan step status, yfinance crumb fix

**Position values:** Qty column now shows the dollar value underneath
the share count (qty × price). No more mental math.

**Scan step status:** Dashboard schedule bars now show the current
pipeline step instead of just "Scanning" — e.g. "Running 16 strategies
(30 candidates)", "Specialist ensemble (15 candidates)", "AI selecting
trades (15 shortlisted)". Polls every 3 seconds via `/api/scan-status/<id>`.
New `scan_status.py` module writes per-profile step files. Cleared when
scan completes.

**yfinance Invalid Crumb fix:** Yahoo rotates session cookies, causing
401 errors that disabled the earnings filter. Added auto-reset of
yfinance's cookie cache when "Invalid Crumb" errors are detected.
Rate-limited to once per 5 minutes.

---

## 2026-04-17 — Multiple silent failures fixed: news, prices, yfinance crashes, MAGA mode

**Severity:** high — AI was making decisions with missing data

**Problems found and fixed:**

1. **Alpaca news API 401s (silent):** Every news fetch was failing with
   "Unauthorized" because the subscription doesn't include the news
   endpoint. The system silently returned empty arrays — AI saw no news
   for any symbol. **Fix:** `fetch_news()` redirected to yfinance news
   (which works and was already used elsewhere in the pipeline).

2. **Political sentiment JSON truncation:** max_tokens=512 was too small
   for the political context response, causing JSON parse errors and
   the AI losing political context. **Fix:** bumped to 1024.

3. **yfinance thread-safety crash:** `yf.download()` uses a shared
   global dict internally that isn't thread-safe. With 10 profiles
   running in parallel, this caused `RuntimeError: dictionary changed
   size during iteration` and crashed entire scan cycles.
   **Fix:** new `yf_lock.py` module wraps all `yf.download()` calls
   in a threading lock. All 10 call sites migrated.

4. **MAGA mode scanner using yfinance batch download:** Still using
   `yf.download(universe)` for 100+ symbols instead of Alpaca bars.
   This caused the "possibly delisted" errors for valid symbols
   (GPS, SQ, SKX) and was the source of the thread-safety crashes.
   **Fix:** migrated to per-symbol `get_bars()` via Alpaca.

5. **Price=0 causing trades to silently not execute:** The AI would
   select a trade (visible in "TRADES SELECTED" on the dashboard)
   but execution silently skipped it because the candidate's price
   was 0 from a failed fetch during strategy scoring. The user sees
   "BUY CRGY" in the brain panel but no trade happens and no error
   appears. **Fix:** price is now verified and re-fetched at the
   shortlist stage before sending to AI. Candidates without a valid
   price are filtered out before wasting an AI call. Execution path
   also re-fetches as a final safety net with a logged warning.

6. **Price fetcher returning 0 silently:** Virtual position P&L showed
   phantom losses when price fetch failed. **Fix:** tries Alpaca bars,
   then Alpaca last trade, then logs a warning — never silently
   returns 0 without explanation.

7. **Earnings calendar logging at debug level:** Failures to check
   earnings dates were invisible. **Fix:** bumped to warning level.

8. **Crisis detector event cluster check:** Failed silently.
   **Fix:** logs warning.

**21 missed trades recovered:** The price=0 bug caused trades the AI
recommended to not execute across multiple profiles. All 21 were
manually executed at current market prices.

---

## 2026-04-17 — Bad account allocation caused 3 data wipes in 2 days

**Severity:** critical — user lost all accumulated trading data three times

**What happened:** When setting up 10 virtual profiles across 3 Alpaca
paper accounts ($1M each), the initial allocation put $1.625M of
virtual capital on a single $1M Alpaca account. This was compounded by
moving profiles between accounts after they had open positions,
creating orphaned trades on the wrong accounts and "account_rebalance"
sells that polluted trade history with non-strategy exits.

**The mistakes, in order:**
1. Created all profiles without thinking about which Alpaca account
   each should use. The $1M Large Cap profile landed on the same
   account as three other profiles totaling $625K — $1.625M virtual
   on a $1M account.
2. Attempted to fix by moving profiles between accounts while they
   had open positions. This created orphaned positions on the old
   account and forced-close trades logged as "account_rebalance"
   that would have corrupted win rate, P&L, and self-tuning data.
3. Each fix required wiping trade data to get back to clean state.
   Total data wipes: 3 (April 15 evening, April 16 afternoon,
   April 17 morning).

**Root cause:** Failure to plan account allocation BEFORE creating
profiles. The allocation should have been the FIRST step, not an
afterthought corrected live with open positions.

**Correct allocation (what we should have done from the start):**
```
Account 1: Large Cap 1M ($1M) = 100% (dedicated)
Account 2: Mid Cap + Mid Cap 25K + Mid Cap 500K = $625K = 62%
Account 3: Everything else = $525K = 52%
```
No account exceeds its Alpaca balance even at 100% utilization.

**Lesson:** When setting up virtual profiles on shared broker accounts:
1. Plan the allocation on paper first — total virtual capital per
   account must not exceed the account balance
2. NEVER move a profile between accounts while it has open positions
3. If allocation must change, close positions on the old account
   first, then move, then wipe that profile's trade history
4. A few hours of planning saves days of lost data

**Data impact:** All 10 profiles start from zero as of 2026-04-17.
No historical trade data survives. The system is now correctly
allocated and collecting clean data going forward.

---

## 2026-04-16 — Critical fix: virtual profiles sized against Alpaca's balance, not their own

**Severity:** critical — virtual profiles with $25K capital were buying $176K of stock

**Symptom:** Mid Cap 25K profile showed cash of -$151,074. Small Cap 25K showed -$12,492.

**Root cause:** `trade_pipeline.py` line 190 and 641, and `trader.py` lines 43-44 and 208 called `get_account_info(api)` and `get_positions(api)` passing only the API client but NOT `ctx`. Without `ctx`, the virtual interception in `client.py` never fired — the pipeline saw Alpaca's $1M account balance and sized positions accordingly.

**Fix:** All 5 call sites now pass `ctx=ctx`:
- `trade_pipeline.py:190` — `get_account_info(api, ctx=ctx)`
- `trade_pipeline.py:641-642` — both `get_account_info` and `get_positions`
- `trader.py:43-44` — `execute_trade` path
- `trader.py:208` — `check_exits` path

**Data impact:** Profiles 5 (Small Cap 25K) and 6 (Mid Cap 25K) had corrupted trade data from oversized positions. Both were wiped clean and reset to their $25K starting balance. All other profiles were unaffected — their trade history is intact.

**Lesson:** The virtual account layer requires that EVERY code path reading equity or positions passes `ctx` through to `client.py`. Added this as an invariant to watch for in future code changes.

---

## 2026-04-16 — Virtual Account Layer (broker decoupling)

**Severity:** architectural — major new capability

**What it enables:** Unlimited virtual trading profiles sharing the
same 3 Alpaca paper accounts. Each virtual profile has its own
starting capital, positions, P&L, and strategy — all tracked
internally. Alpaca is used only for order execution and price quotes.

**Architecture:**
- **Internal position ledger** (`journal.get_virtual_positions()`) —
  computes net positions from the trades table via FIFO lot tracking.
  Returns the exact same dict shape as `client.get_positions()` so
  every downstream consumer works unchanged.
- **Virtual equity tracker** (`journal.get_virtual_account_info()`) —
  computes equity, cash, buying power from trade flows + initial
  capital. `cash = initial_capital - sum(buy_costs) + sum(sell_proceeds)`.
- **Profile-to-account mapping** — new `alpaca_accounts` table holds
  named broker connections. Multiple profiles can reference the same
  account via `alpaca_account_id` FK. `is_virtual=1` flips the profile
  to use internal data instead of Alpaca reads.
- **Single interception point** (`client.py`) — `get_positions()` and
  `get_account_info()` check `ctx.is_virtual` and route to the
  internal ledger when true. Because trader.py, trade_pipeline.py,
  multi_scheduler.py, and views.py all call through client.py, this
  one change makes the entire pipeline virtual-aware.
- **Virtual-aware reconciliation** — virtual profiles use the internal
  ledger as ground truth (not Alpaca's combined view of shared accounts).
- **Settings UI** — new "Alpaca Accounts" section for managing broker
  connections. "Create Profile" form has a dropdown to select a shared
  account + starting capital input. Virtual profiles show "(Virtual)"
  badge on the dashboard.

**Backward compatibility:** Existing profiles have `is_virtual=0` and
`alpaca_account_id=NULL`. Zero behavior change — they continue using
per-profile Alpaca keys and reading positions/equity from Alpaca.

**Schema:**
```sql
CREATE TABLE alpaca_accounts (id, user_id, name, keys, base_url);
ALTER TABLE trading_profiles ADD COLUMN alpaca_account_id INTEGER;
ALTER TABLE trading_profiles ADD COLUMN is_virtual INTEGER DEFAULT 0;
ALTER TABLE trading_profiles ADD COLUMN initial_capital REAL DEFAULT 100000;
```

**Tests:** 26 new (test_virtual_positions.py: 15, test_virtual_account.py: 11)
covering FIFO lots, partial sells, unrealized P&L, equity math,
output shape compatibility, price fetcher fallbacks, UserContext defaults.

**Test count:** 583 passing.

---

## 2026-04-15 — Scaling projection v4: side-by-side market vs limit columns

**Severity:** UX — final iteration on the Scalability tab

**User pushback on v3:** "Why is the system OK with losses vs switching
to limit orders, is it really nonstandard to use limit orders?"

**The honest answer:** it's a real tradeoff, not a clear winner.
Limit orders cut slippage by ~60% but can miss fills entirely
on momentum moves. The "right" choice depends on strategy style.

**Fix:** stop picking for the user. Show BOTH execution styles
side-by-side at every capital tier so they can compare and decide.

**New table layout:**
```
                       │ If Market Orders │ If Limit Orders │
Capital   Profile      │ Slippage Return  │ Slippage Return │
$10K      Small Cap    │  0.336%  -0.09%  │  0.134%   +0.11%│
$50K      Small Cap    │  0.751%  -0.51%  │  0.300%   -0.05%│
$100K     Small Cap    │  1.062%  -0.82%  │  0.425%   -0.18%│
$500K     Mid Cap      │  0.751%  -0.51%  │  0.300%   -0.05%│
$1M       Mid Cap      │  1.062%  -0.82%  │  0.425%   -0.18%│
$10M      Large Cap    │  1.062%  -0.82%  │  0.425%   -0.18%│
```

Limit columns are tinted green to make the comparison obvious. The
footer notes that limits are an option but come with their own
tradeoff (missed fills on momentum moves) and points to the profile
settings toggle.

**Code changes:**
- `project_scaling()` returns `slippage_market_pct` + `slippage_limit_pct`
  + `return_market_pct` + `return_limit_pct` + CIs for both.
- Calibration backs out the right baseline based on what the user
  is currently using (so we never double-apply or miss the limit
  benefit).
- Removed `_LIMIT_ORDER_CAPITAL_THRESHOLD` — no automatic switch at
  any tier; both shown everywhere.
- Removed `uses_limit_orders` per-row flag (no longer needed).
- Slippage growth no longer clipped at 0 — improvements (e.g.
  switching to limits at current scale) properly INCREASE projected
  return.
- Template adds a header row with `colspan` grouping the two
  execution columns.

**Tests** (`test_scaling_projection.py` — 20, replaced ExecutionStyleAdjustment
with BothExecutionStylesAlwaysShown):
- Every row has both market and limit columns
- Limit slippage always lower than market
- Limit/market ratio stays at ~0.40 across all scales
- Baseline calibration correct whether user is on market or limit
  orders (back-implied properly in both directions)

**Test count:** 528.

---

## 2026-04-15 — Tooltip z-index fix (mounted to body via JS)

**Symptom:** tooltips on the Scalability table appeared as a thin
sliver — clipped by the parent `overflow-auto` container.

**Fix:** new JS in `base.html` mounts a single tooltip element on
`<body>`, positioned via `getBoundingClientRect` and `position: fixed`.
Escapes ALL parent overflow constraints. Hides on scroll. The old CSS
pseudo-element approach (`.tip:hover::after`) is stripped + defensively
suppressed in case browser cache lingers. CSS link gets a
`?v=20260415-tooltip-fix` cache-buster.

---

## 2026-04-15 — Scaling model v3: real-world migration ladder + execution adjustment

**Severity:** medium — UX/accuracy of a high-visibility planning tool

**User feedback (v2 wasn't right either):** "Why are you using the same
profile (small cap) for all the different levels? That doesn't make
sense. Isn't this supposed to show the scalability in a real way?"
Plus: "Why are we referencing internal documentation on all these rows?"
Plus: "The tooltips for est. slippage and universe appear to be showing
up behind the layer above."

**Real-world model:** at each capital level, project what would
*actually happen* — not "if you stubbornly stayed Small Cap forever"
(v2) and not "magically blended universe" (v1). Three real effects
compound:

1. **Square-root market impact.** Larger orders cost more, scaling as
   `√(position_size)`. (Almgren-Chriss.)
2. **Tier migration.** $250K+ rationally migrates Small → Mid; $5M+
   migrates Mid → Large. Each tier offers ~10× more daily volume per
   name, which offsets ~√10 ≈ 3.16× of capital growth.
3. **Order-execution style.** Above $100K any rational operator uses
   limit orders, which cut realized slippage by ~60%. Cap-bracket
   institutional norm.

The combined effect: with the full real-world playbook, slippage stays
roughly flat across the entire scale range. Realistic example for a
user calibrated at 0.336% baseline (Small Cap, market orders, 6 fills):

```
Capital  Profile     Orders   Slippage   Return
$10K     Small Cap   market    0.336%    -0.09%
$50K     Small Cap   market    0.751%    -0.51%
$100K    Small Cap   limit     0.425%    -0.18%   ← order type switches
$500K    Mid Cap     limit     0.300%    -0.09%   ← profile migrates
$1M      Mid Cap     limit     0.425%    -0.18%
$10M     Large Cap   limit     0.425%    -0.18%   ← profile migrates again
```

That's what real institutional execution looks like. The previous v2
model showed slippage exploding to 10%+ at $10M because it pretended a
small-cap operator would still be running small-cap names — which no
sane operator would.

**Code changes:**
- `scaling_projection.project_scaling()` — added `use_limit_orders_now`
  parameter, applies 0.40× slippage multiplier when projection assumes
  switch to limit orders (only if user isn't already using them).
- `_LIMIT_ORDER_CAPITAL_THRESHOLD = $100K`, `_LIMIT_ORDER_SLIPPAGE_MULT = 0.40`.
- `views.py` reads the profile's `use_limit_orders` setting and
  threads it through.
- `templates/performance.html` adds an "Orders" column showing
  Market vs Limit at each capital level.
- Migration notes use plain English: "At this scale, you'd switch
  to a Mid Cap profile. The bigger universe gives you ~10× more daily
  volume per name." No internal-doc references like "SCALING_PLAN.md."
- Footer explanation lists the three compounding effects in user
  terms — no jargon, no formulas.

**Tooltip z-index fix:**
- `.tip` tooltips were CSS pseudo-elements being clipped by parent
  `overflow-auto` on the Scalability table — visible only as a
  thin sliver.
- New JS in `base.html` mounts a single tooltip element on `<body>`,
  positioned via `getBoundingClientRect` and `position: fixed`.
  Escapes ALL parent overflow constraints. Hides on scroll so it
  doesn't float orphaned.
- Old CSS `.tip:hover::after` / `::before` stripped to avoid
  double-rendering.

**Tests** (`test_scaling_projection.py` — 19 total, +5 new):
- `test_above_100k_uses_limit_orders_when_currently_market`
- `test_limit_order_adoption_lowers_slippage_at_threshold`
- `test_already_using_limit_does_not_double_apply_benefit`
- `test_limit_order_note_in_migration_row`
- `test_no_internal_doc_references_anywhere` — sweeps every output
  string for `.md` and `scaling_plan` so internal doc names can't
  silently leak to the UI again
- Updated `test_migration_offsets_capital_growth` — pinned to the
  real-world ratio (~1.27×) rather than the without-execution-adjustment
  ratio (~3.16×)

**Test count:** 527 (was 525 + 5 new − 3 obsolete from earlier model
revisions).

---

## 2026-04-15 — Scaling model: removed fake universe shifts, made monotonic

**Severity:** medium — the v1 sqrt model fixed the linear bug but
introduced its own quirk: non-monotonic projections from cross-profile
universe shifts. User screenshot showed slippage going
`0.336% → 0.752% → 0.336% → 0.752% → 1.063%`, with universe rows
labeled "drops {micro} (improves liquidity)" that didn't apply to a
single-profile view.

**Root cause:** the projection assumed the universe SHIFTS as capital
grows, blending across all cap tiers. That's a valid model for
"if I were running the whole system at $X AUM," but it's wrong when
the user is viewing a *single profile* (e.g. Small Cap). A Small Cap
profile only ever trades small caps — the universe is FIXED by
`market_type`. The "shift to large at $1M" is a system-level
recommendation in `SCALING_PLAN.md`, not a per-profile projection.

**Fix:**
- `project_scaling()` now uses the profile's `market_type` as a fixed
  singleton universe at every ladder rung.
- Slippage formula is pure `base × sqrt(scale_mult)` — guaranteed
  monotonic. No more confusing up-down-up artifacts.
- New `_MAX_CAPITAL_BY_MARKET_TYPE` table encodes per-tier soft
  capacity (micro $50K, small $250K, mid $5M, large $50M+, crypto $1M).
- Each row gets a `warnings[]` and `exceeds_capacity` flag. Once
  capital exceeds the soft max, the row warns the user to migrate
  capital to a larger-cap profile per SCALING_PLAN.md — instead of
  fudging the slippage number lower.

**Sample output for a Small Cap profile** (6 trades, 0.336% baseline):
```
$10K   0.336%  small                  ← soft max $250K
$50K   0.751%  small
$100K  1.062%  small
$500K  2.376%  small  EXCEEDS CAPACITY → migrate to mid/large
$1M    3.360%  small  EXCEEDS CAPACITY
$10M  10.625%  small  EXCEEDS CAPACITY
```
Now monotonic, honest about per-tier capacity, and the universe column
shows the actual profile rather than a fictional cross-profile blend.

**Tests** (`test_scaling_projection.py` — 17 total, +3 new):
- `test_slippage_monotonic_across_ladder` — guards against
  reintroducing universe-shift artifacts
- `test_small_cap_universe_stays_small_at_all_scales` — fixed-
  universe invariant
- `test_capacity_warning_when_exceeding_soft_max` — exercises the
  `exceeds_capacity` flag and warning text
- `test_market_type_aliases_normalize` — `smallcap` and `small`
  produce identical projections
- `test_mid_cap_has_higher_capacity_than_small` — sanity check on
  the per-tier capacity ladder
- Updated `test_100x_capital_gives_10x_slippage_pure_sqrt` — pure
  sqrt(100)=10 instead of the v1's universe-fudged value

**Test count:** 525 (was 522 + 3 new − 0 removed).

---

## 2026-04-15 — Replaced broken linear scaling model with sqrt-impact + universe-aware ladder

**Severity:** medium — UI was showing dangerously misleading projections

**Symptom:** Performance > Scalability > "Scaling Projection" tab showed
slippage of >10% at $1M AUM. That's plausible for trading penny stocks
with no risk management, but absurd for our system which rotates universe
as it scales. The number was scary enough to throw off planning, and it
was wrong.

**Root cause:** the Jinja template did the math inline:
```
slippage_at_scale = base_slip × (1 + (mult - 1) × 0.1)
return_at_scale   = base_return - base_slip × (mult - 1) × 0.05
```
Three flaws:
1. **Linear**, not square-root. Real market impact is sub-linear in trade
   size (Almgren-Chriss).
2. **Ignored universe changes.** `SCALING_PLAN.md` already documents
   that we drop micro at $100K, drop small at $1M, etc. The model
   projected as if the system kept slamming the same illiquid names.
3. **Arbitrary constants.** `+0.1` per multiplier and `0.05×slip` for
   return decay had no empirical or theoretical basis.

**Fix:** new `scaling_projection.py` module implements:
- Square-root market impact: `scaled = base_slip × √(scale_mult / liquidity_factor)`
- Universe-change ladder per capital tier (micro dropped at $100K, small
  dropped at $10M, etc.) with empirical $ADV averages for each cap tier
- Confidence intervals scaled to sample size (n<10 = ±100%, n≥100 = ±10%)
- Three data-quality states: `insufficient` (no fill data → show N/A),
  `modeled` (small sample → wide CIs), `calibrated` (≥30 trades → tight CIs)

**Realistic example output** (50 trades with 0.05% baseline slippage,
small-cap profile):
```
Capital     Slippage   CI            Universe                  Return
$10K        0.050%     [0.037,0.062] micro,mid,small           +12.00%
$100K       0.050%     [0.037,0.062] large,mid,small (-micro)  +12.00%
$1M         0.158%     [0.119,0.198] large,mid,small           +11.89%
$10M        0.281%     [0.211,0.351] large,mid (-small)        +11.77%
```
Note how the $100K row's slippage stays at 0.050% — the 10× capital
increase is exactly offset by the universe shift to more liquid names.
This is the kind of insight the broken linear model erased.

**Wired in:**
- `views.py` performance route loads `_gather_trades(db_paths)` and
  calls `project_scaling()` with the selected profile's market_type
- `templates/performance.html` Scalability tab now renders the table
  from `scaling.rows`, shows confidence intervals, lists per-tier
  warnings (universe drops, position-vs-volume cautions), and
  surfaces the model formula in the footer

**Tests** (`test_scaling_projection.py` — 14 tests):
- Square-root scaling: 4× capital → ~2× slippage, NOT 4×
- 100× capital → < 7× slippage when universe shifts (regression guard
  against the broken linear formula)
- Universe correctly drops micro at $100K, small at $10M
- Crypto universe stays crypto at all capital levels
- CIs widen with small samples (n=5 → ±100%; n=150 → ±10%)
- Insufficient-data path returns flag + message instead of misleading numbers
- Net return projection only deducts the *additional* slippage cost,
  not arbitrary 5× decay
- **Hard regression bound:** $1M slippage with 0.05% baseline must be < 1%

**Test count:** 522 (was 508 + 14).

---

## 2026-04-15 — Conviction take-profit override (prevent capping runaway winners)

**Severity:** feature — opt-in per profile, default OFF

**Motivation:** IONQ this morning sold at +20% TP, then the AI immediately
wanted back in at a slightly higher price. That's the IONQ scenario —
fixed TP caps the upside when a strong trend is actually still running.
A trailing stop would have ridden the move further; fixed TP pays bid-ask
spread + slippage twice for no extra return.

**Design:** new per-profile flag `use_conviction_tp_override`. When on,
a long position's fixed take-profit is SKIPPED if ALL three conditions
hold:
1. Most recent AI prediction confidence for the symbol >= `conviction_tp_min_confidence` (default 70)
2. Latest ADX >= `conviction_tp_min_adx` (default 25) — trend has actual strength
3. Current close >= previous bar's high — trend is still intact right now

When skip fires, the ATR trailing stop continues to manage the exit. If
the trend reverses, trailing stop catches it. If it keeps running, we
keep the gains.

**What is NEVER overridden (safety):**
- Stop-loss — always fires
- Short-position take-profit — shorts profit on fast reversals, not trends

**Files:**
- `conviction_tp.py` — new module: pure predicate + DB/bars IO wrapper
- `portfolio_manager.check_stop_loss_take_profit` — new
  `conviction_tp_skip` kwarg (optional callable)
- `trader.check_exits` — builds the skip predicate when the profile
  has the override enabled
- `user_context.UserContext` — 3 new fields (default OFF)
- `models.py` — 3 new ALTER TABLE migrations + build_user_context loader
- `views.py` — settings POST handler persists the 3 new fields
- `templates/settings.html` — new checkbox + 2 sliders under Trailing
  Stops section with tooltip explaining tradeoff

**Tests** (`test_conviction_tp.py` — 17 tests):
- Pure predicate: all conditions true → True; any one false → False;
  None/missing inputs → False (safe default: don't skip)
- Integration with `check_stop_loss_take_profit`: skip fn prevents
  long TP; returning False still triggers TP; stop-loss NEVER skipped;
  short TP NEVER skipped; no-skip-fn preserves legacy behavior
- DB lookups: most recent confidence wins; missing DB returns None;
  empty path returns None
- UserContext defaults: off, 70%, 25 (unchanged behavior for existing
  profiles)

**Test count:** 508 (was 491 + 17).

**Self-tuning note:** The override is NOT auto-tuned by the existing
self-tuning system. That system adjusts numeric thresholds
(confidence, stop/TP %), not boolean strategy flags. Auto-toggling
can be added later once we have 15-20 TP events to compare
"counterfactually would have kept running" vs "reversed" — a
premature flip on a 3-trade sample would do more harm than good.

---

## 2026-04-15 — Dashboard expand-row state preserved across auto-refresh

**Severity:** low UX — annoying, not broken

**Symptom:** Dashboard auto-refreshes Open Positions every 15s by fetching
the server-rendered HTML and replacing the wrapper. Any row the user
had expanded to read AI reasoning collapsed on refresh — mid-sentence.

**Fix:** `_trades_table.html` macro adds `data-symbol` on the summary
row. Dashboard JS `refreshPositions()` captures the set of expanded
symbols before the swap, then reapplies expansion state (and the
caret icon) afterward. State is by symbol, so it survives add/remove
of positions as well.

---

## 2026-04-15 — Dashboard: Open Positions now use rich format, Recent Trades removed

**Severity:** low (UX improvement, reduced duplication)

**Symptom & rationale:** The dashboard was double-duty — Open Positions
(live Alpaca data) plus a slim Recent Trades table, both competing for
space. The Recent Trades duplicated what `/trades` already does better
(full history, filters, expandable reasoning). Meanwhile Open Positions
lacked the AI metadata that made `/trades` useful.

**Fix:**
- Open Positions now render through the shared
  `_trades_table.html` macro. Each row is click-to-expand; the
  expanded panel shows Current Price, Market Value, AI Reasoning,
  Stop/Target, and Slippage.
- `_enriched_positions(ctx, profile_id)` — new helper that merges
  Alpaca's live position data with the most recent matching row in
  the profile's `trades` table, pulling in `ai_reasoning`,
  `ai_confidence`, `stop_loss`, `take_profit`, `decision_price`,
  `fill_price`, `slippage_pct`.
- Recent Trades table removed from the dashboard. Replaced with a
  small "View full trade history →" link that filters `/trades`
  by the profile.
- `/api/positions-html/<id>` — new partial endpoint returning the
  server-rendered positions block. The 15-second auto-refresh fetches
  HTML instead of rebuilding in JS, so the expandable markup can't
  drift from the template.
- Macro extended: expanded panel now shows Current + Market Value
  when the row is an open position (detected by `current_price`
  being set).

**Tests** (`test_enriched_positions.py` — 6 tests):
- Positions gain AI metadata from the matching open trade
- Most-recent trade wins when symbol has been re-entered
- Positions without any matching trade still render (manual Alpaca
  fills don't crash the dashboard)
- Missing DB doesn't crash
- Short positions get `side='sell'` with absolute qty
- Empty positions list returns empty list (not error)

**Test count:** 491 (was 485 + 6).

---

## 2026-04-15 — Unified dashboard + /trades trade-history display

**Severity:** low (UX consistency, DRY refactor)

**Symptom:** Dashboard had a slim 6-column trade table (Time / Symbol /
Side / Qty / Price / P&L) while `/trades` had the richer 9-column
expandable version (Time / Profile / Symbol / Side / Qty / Price / AI
Conf / P&L + expand row showing AI reasoning, stop, target, slippage).
Two copies of similar Jinja meant bug fixes landed on one and not the
other.

**Fix:**
- New `templates/_trades_table.html` — single Jinja macro
  `render_trades(trades, show_profile, empty_message)` owning all
  trade-row markup including expand-on-click details row.
- `templates/trades.html` and `templates/dashboard.html` now both
  `{% import "_trades_table.html" as trades_tpl %}` and call the macro.
- Dashboard calls with `show_profile=False` (it's already per-profile);
  `/trades` calls with `show_profile=True`.
- `colspan` auto-adjusts to match column count.

**Net effect:** dashboard now shows AI confidence + expandable AI
reasoning, stop/target, slippage on every trade, matching `/trades`.
Future UI tweaks land in one place.

**Tests** (`test_trades_table_shared.py` — 12 tests):
- AI confidence, reasoning, stop/target, slippage all render
- Expand-caret present
- `show_profile` toggle adds/removes Profile column AND adjusts colspan
- Empty-state custom + default messages
- P&L rendering: realized (closed), unrealized (open-with-mark), open-no-mark

**Test count:** 485 (was 473 + 12).

---

## 2026-04-15 — Pending Alpaca orders now visible on dashboard (Task 18.4)

**Severity:** medium (UX / operational visibility)

**Symptom:** After-hours order submissions queue in Alpaca as `accepted`
or `new` and don't fill until the next session. Dashboard showed only
filled positions, so a user couldn't tell "scheduler has orders waiting
for market open" from "scheduler produced nothing this cycle." Silently
confusing.

**Fix:**
- `views._safe_pending_orders(ctx)` — defensive wrapper around
  `api.list_orders(status="open")` with float coercion and
  exception-to-empty-list fallback.
- Dashboard renders a new "Pending Orders" table between Open Positions
  and Recent Trades, showing symbol / side / qty / order type / limit
  price / status / submitted timestamp / TIF.
- `/api/portfolio/<id>` returns `pending_orders`; JS auto-refresh every
  15s updates the table alongside positions.
- Hidden entirely when the list is empty (no dead UI).

**Tests** (`test_pending_orders.py` — 5 tests):
- Happy path: accepted limit buy renders with correct shape
- Market orders produce `limit_price=None`
- Garbage numeric fields coerce safely instead of crashing
- API exception → empty list, not 500
- `list_orders` is called with `status="open"` (filters out fills)

**Test count:** 473 (was 468 + 5).

---

## 2026-04-15 — Cleaned up stale `/opt/quantops/` directory on server (Task 20.5)

**Severity:** low (operational hygiene / prevents future confusion)

**Symptom:** Earlier today I wasted a minute on the server when `find`
surfaced a stale `aggressive_trader.py` at `/opt/quantops/` (no "ai")
— an abandoned pre-refactor codebase from March 27. The active service
runs at `/opt/quantopsai/`. Old path had a disabled `quantops.service`
systemd unit, not inactive since 2026-03-28.

**Fix:** `systemctl disable quantops.service`, removed the unit file,
`daemon-reload`, `rm -rf /opt/quantops/`. Verified `/opt/` now contains
only `quantopsai/`. No running service referenced the stale tree.

---

## 2026-04-15 — Strategy SELL-bias starved Small Cap of trades for 4+ days

**Severity:** critical — profile opened zero trades despite scanning every 15 min

**Symptoms:** Small Cap profile scanned continuously (616 AI predictions
across 2026-04-13 to 2026-04-15) but opened **zero trades**. Every
prediction returned `HOLD` with `confidence=0`. Mid Cap and Large Cap
were also affected — their shortlists were 11/12 and 15/15
`STRONG_SELL` respectively; only a stray `STRONG_BUY` had let Mid Cap
open any positions, and not recently.

**Where the prior "working as intended" call was wrong:** Past
evaluations chalked this up to a genuinely bearish universe. It was
actually a labeling bug — the screener was pre-tagging nearly every
candidate `STRONG_SELL` before the AI even saw it, the specialist
ensemble (Phase 8) saw the `STRONG_SELL` input and agreed, and the AI
correctly concluded "no edge across the board." The loop looked
convincing because every layer "agreed."

**Root cause:** Each size-specific strategy module
(`strategy_small.py`, `strategy_mid.py`, `strategy_large.py`,
`strategy_micro.py`) is a LONG-ONLY entry engine, but several of its
internal rules emitted `signal="SELL"` whenever the **exit condition
for a hypothetical existing long** was true. Examples:

- `mean_reversion_strategy`: SELL if `price >= sma_20` OR `rsi > 55`
  (fires on ~60-70% of any universe)
- `momentum_continuation_strategy`: SELL if `price < sma_20`
- `ma_alignment_strategy`: SELL if `price < sma_20`
- `pullback_support_strategy`: SELL if `price < sma_50`
- `dividend_yield_strategy`: SELL if `rsi > 55`
- `penny_reversal_strategy`: SELL if `price >= sma_10` OR `rsi > 50`
- `volume_explosion_strategy`: SELL if `vol_ratio < 2 and rsi > 60`
- `sector_momentum_strategy`: two separate bogus SELL branches

Those comments literally say `EXIT --` but the code emits a SELL
signal, which `multi_strategy.aggregate_candidates()` then interprets
as bearish sentiment. A typical stock accumulated 2+ SELL votes → score
≤ -2 → label `STRONG_SELL`. AI then declined everything.

**Fixes:**

1. **Aggregation respects short-selling flag.** `multi_strategy.aggregate_candidates()`
   now coerces SELL votes to HOLD (and zeroes their score contribution)
   when the profile has `enable_short_selling=False`. Defensive — all
   current profiles have shorting on, but this closes the class of bug
   for any future long-only profile.
2. **Stripped the broken SELL branches.** Replaced ~12 "exit-as-SELL"
   branches with HOLD returns across all four size strategy files.
   Legit bearish setups preserved (MACD bearish cross, 10-day-low
   break, failed gap, falling-knife 10-consecutive-red-days, SPY
   overbought ≥75).

**Why the specialist ensemble didn't catch it:** The ensemble receives
the already-`STRONG_SELL`-labeled shortlist as input. It's a
second-layer consensus model, not a first-principles re-evaluator — its
job is to confirm or veto, not to re-score from scratch. GIGO.

**Why no prior test caught it:** The existing `test_multi_strategy.py`
fixtures passed explicit `signal` values into fake strategies; they
never exercised the "what happens when a real strategy emits SELL
from an exit-condition" path.

**Tests** (`test_strategy_sell_bias_fix.py` — 18 tests):
- Aggregation: SELL → HOLD when shorting off, pass-through when on,
  BUY votes untouched by the flag
- `mean_reversion` returns HOLD at RSI 60 and above-SMA, still BUY
  when truly oversold
- `momentum_continuation`, `sector_momentum`, `pullback_support`,
  `dividend_yield`, `ma_alignment`, `relative_strength`,
  `volume_explosion`, `penny_reversal` all return HOLD (not SELL)
  in the previously-broken conditions
- **Preserved legit bearish signals:** 10-day-low break still SELLs,
  MACD bearish cross still SELLs, 10-consecutive-red-days still SELLs
- End-to-end: diverse universe with no SELL votes produces zero
  `STRONG_SELL` labels (regression guard against the Small Cap freeze)

**Verification:** Small Cap's next scan cycle post-deploy should show
a mix of signal labels (not 100% `STRONG_SELL`) and begin evaluating
BUY candidates. Actual trade execution still gated behind the AI
(Phase 1-10 stack), which now has real information to decide on.

**Test count:** 468 (was 450 + 18).

---

## 2026-04-15 — Migrated market data from yfinance to Alpaca (Algo Trader Plus)

**Severity:** architectural improvement (prevents the class of bug from
yesterday's 30-min hang; not fixing a new regression)

**Context:** yfinance is an unofficial Yahoo scraper; during market open
Yahoo throttles and returns 10-sec timeouts on many symbols. Yesterday
this hung the screener for 30+ minutes and blocked exits behind it,
nearly costing ~$100 of locked-in profit on HOOD and IONQ.

**Upgrade:** subscribed to Alpaca Algo Trader Plus ($99/mo) for SIP feed
and unlimited historical bars. Updated main `.env` with account-level
master API key that has the subscription active.

**Code migration:**
- `market_data.get_bars()` now tries Alpaca first, falls back to
  yfinance. Crypto symbols (containing `/`) bypass Alpaca directly —
  Alpaca's equity endpoint doesn't serve crypto.
- `screener.screen_dynamic_universe()` now uses Alpaca's
  `get_snapshots()` batch endpoint (up to 200 symbols per call) to
  filter by price + volume. The previous `yf.download()` path remains
  as a fallback when the Alpaca snapshot call fails or raises.

**Measured speedup:**
- Single `get_bars` call: 10s timeouts → 200ms (50× faster)
- Full dynamic screener: 30 min → 853 ms (**~2,100× faster**)
- First live cycle post-restart: Small Cap Scan & Trade completed
  in 166 seconds (well inside the 15-min interval)

**Tests** (`test_alpaca_data_migration.py` — 13 tests):
- `_limit_to_days` calendar window math
- Alpaca success → lowercase OHLCV columns + US/Eastern tz
- Alpaca over-fetch respects caller's `limit` via `.tail()`
- Alpaca empty / exception / missing client → yfinance fallback
- Crypto symbols skip Alpaca entirely, slash→dash for yfinance
- Screener Alpaca success path → filtered symbols
- Screener Alpaca failure → yfinance fallback invoked
- **Contract guards:** source inspection ensures the Alpaca-before-yfinance
  ordering can't silently regress in either `market_data.get_bars` or
  `screener.screen_dynamic_universe`.

**Test count:** 450 (was 437 + 13).

---

## 2026-04-15 — Exits blocked behind hung scan (realized-P&L risk)

**Severity:** critical — positions past take-profit thresholds weren't selling

**Symptoms:** Mid Cap Scan & Trade hung for 30+ minutes during market
open. User noticed positions should have hit take-profit but nothing
was firing. Manual exit-check via SSH triggered HOOD (+10.2%) and IONQ
(+20.3%) sells that the scheduler had been sitting on.

**Root cause:** `run_segment_cycle` ran tasks in order `scan → exits`.
When the scan hung (yfinance timeout storm during market open, see
below), exit checks never got a chance. Take-profit and stop-loss
triggers are only meaningful if they fire within minutes of being
hit; gating them behind a 30-minute hung scan means P&L evaporates.

**Fixes:**
1. **Exits run BEFORE scan** — reordered `run_segment_cycle` so
   `_task_check_exits`, `_task_cancel_stale_orders`, and
   `_task_update_fills` fire first. Exits are ~1-5 seconds per profile,
   cheap, and must never be blocked by a slow scan pipeline downstream.
2. **Exit interval shortened from 15 min → 5 min** — `INTERVAL_CHECK_EXITS`
   was matching the scan interval; now it's independent and tight enough
   that TP/SL triggers fire within 5 min of being hit.
3. **Dynamic screener budget + disk cache** — the hang root cause was
   yfinance getting hammered during market open (40+ failed downloads
   at 10-sec timeouts each). Added `_DYNAMIC_YF_BUDGET_SEC = 180` hard
   wall-clock budget that abandons yfinance after 3 min and falls back
   to stale cache or curated fallback. Cache now persists to
   `dynamic_screener_cache.json` so process restarts don't force a
   re-scan.
4. **Trailing stop NoneType crash** — `check_trailing_stops` failed with
   "'NoneType' object is not subscriptable" on symbols where `get_bars`
   returned a malformed DataFrame. Added defensive guards: skip if
   `bars` is None / missing `.empty` / missing required columns /
   NaN ATR.

**Verified live:** at 14:21:02 UTC, Mid Cap's Check Exits completed
in 4.2 seconds — before Scan & Trade even started. Exit checks are now
firewalled from scan failures.

**Tests:** `test_screener_cache.py` — 4 tests covering disk persistence,
stale fallback, and budget constant bounds. Total suite now 437 passing.

---

## 2026-04-14 — Per-profile scheduling (Large Cap starvation bug) + droplet swap

**Severity:** high (profiles could be starved, scheduler would silently skip)

**Bug:** Scheduler tracked `last_run["scan"]` / `last_run["check_exits"]`
/ `last_run["resolve_predictions"]` as a **single global timestamp shared
across all profiles**. When one profile's full cycle (scan + ensemble
+ AI + event tick) overran the 15-minute interval, every other profile
inherited the same "just ran" timestamp and none would be due again for
15 minutes. In practice: Mid Cap took ~5 min, then Small Cap ~5 min,
then Large Cap (last in iteration) was often still starting when the
next interval rolled around — so its cycle got truncated or skipped
entirely. The user observed zero Large Cap trades despite the profile
being enabled.

**Fix:**
- New `profile_runs: Dict[int, Dict[str, float]]` state, keyed by
  profile_id. Each profile gets its own `{scan, check_exits,
  resolve_predictions}` timestamps.
- Helper `_get_profile_runs(pid)` lazily initializes a profile's
  entry on first access.
- The profile-iteration loop now computes `prof_do_scan` /
  `prof_do_exits` / `prof_do_predictions` **per-profile** from that
  profile's own timestamps.
- After each profile's cycle completes, **only that profile's**
  timestamps are stamped — adjacent profiles aren't affected.
- Snapshot remains global (one snapshot per calendar day is the
  correct system-wide behavior).
- Legacy segment-mode branch keeps the old global `last_run` for
  backwards compat; only the profile branch changed.

**Natural staggering:** First-run starts all profiles due simultaneously.
Sequential execution (one at a time, since we're memory-constrained)
means profile 1 finishes at T+5min, profile 2 at T+10min, profile 3 at
T+15min. Each then clocks its own 15-minute interval from there. After
one full warm-up cycle, the three profiles naturally fire at
approximately staggered 5-minute offsets. No explicit offset logic
needed — emerges from sequential execution + independent clocks.

**Secondary: added 1 GB swap to droplet.** The droplet is 1 GB RAM,
1 CPU, no swap — 681 MB used, 281 MB free. A Python memory spike
(large yfinance batch, concurrent AI responses) could OOM-kill the
scheduler with no cushion. `fallocate /swapfile 1G`, `mkswap`,
`swapon`, persisted in `/etc/fstab`. Free + safety. Does not enable
parallel execution, but prevents unexpected OOM kills.

**Tests:** `test_per_profile_scheduling.py` — 5 tests covering
independent clocks, slow-cycle-doesn't-starve-others invariant,
natural staggering from sequential execution, module import
stability, and a source-pattern guard that fails loudly if the
per-profile structure is ever flattened back to globals.

**Test count:** 426 (was 421 + 5).

---

## 2026-04-14 — Dashboard P/L formatting flicker + earnings detector import bug

**Bug A: Unrealized P/L cell flickers between two formats**
- On page load (Jinja-rendered): `-29.70` (no `$`)
- On 5-second auto-refresh (JS-rendered): `$-29.70` (`$` prepended,
  minus sign INSIDE the dollar)
- The two render paths used different format strings for the same cell.
  Looked like the column was changing because it WAS — every refresh.
- **Fix:** standardized both to `+$1,234.56` / `-$29.70` (sign before
  `$`, conventional). Changed in `dashboard.html` template (line 166)
  AND inline JS (line 630). Same fix applied to `trades.html` for the
  unrealized-P/L badge.

**Bug B: `event_detectors.detect_earnings_imminent` imports nonexistent function**
- Imports `get_next_earnings` from `earnings_calendar` — function doesn't
  exist (the actual API is `check_earnings(symbol) -> dict`). Detector
  silently failed every event tick with a warning the user wouldn't see.
- **Fix:** call `check_earnings(sym)` and read `.days_until` from the
  returned dict.
- **Tests:** `test_event_bus.TestEarningsImminentDetector` — 2 tests
  verify the import resolves and the detector handles empty positions.

**Test count:** 421 (was 419 + 2).

---

## 2026-04-14 — Profile switch: Crypto → Large Cap

**Severity:** (not a bug — operational change, logged per changelog policy)

**What changed:** The Crypto profile (id=2) was producing zero trades
despite consuming ~$0.78/day in AI calls because 3 of 4 specialists had
no crypto-relevant data. After the ensemble scoping fix limited crypto
to pattern_recognizer only, we further discussed whether to continue
running crypto at all versus switching to Large Cap, where all 10 phases
of infrastructure apply meaningfully.

**Decision:** Switch. Alpaca Crypto account deleted; new Alpaca Large Cap
paper account created.

**Steps taken on the server:**
1. Profile id=2 renamed to "Crypto (archived)", `enabled=0`, Alpaca keys
   blanked so the scheduler stops trying to authenticate.
   Historical DB (`quantopsai_profile_2.db`) preserved as archival
   record of crypto prediction history.
2. New profile id=4 "Large Cap" created with `market_type='largecap'`,
   `schedule_type='market_hours'`, `enable_short_selling=1`, settings
   mirroring Mid Cap (max_position_pct=0.08, max_total_positions=10).
3. Alpaca credentials encrypted via `crypto.encrypt()` and stored in
   `trading_profiles.alpaca_api_key_enc` / `alpaca_secret_key_enc`.
4. `journal.init_db('quantopsai_profile_4.db')` to create the Large Cap
   profile's database with current schema (including the new
   `recently_exited_symbols` and `ai_cost_ledger` tables from today).
5. Scheduler restarted. New profile is now in the rotation:
   Mid Cap → Small Cap → Large Cap (Crypto no longer iterated).

**Verified:** Alpaca connection live, equity $10,000 paper, status ACTIVE.

**Implication for MONTHLY_REVIEW.md tracker:** the month-1/2/3 review
metrics are now gathered across three equity profiles (Mid, Small,
Large Cap) all using the full 10-phase stack. Historical crypto data
in `quantopsai_profile_2.db` stays archived and does not feed meta-model
training or decay monitoring for the new profile.

---

## 2026-04-14 — Crypto specialist ensemble scoped to pattern_recognizer only

**Severity:** medium (cost + signal quality on crypto)

**Symptoms:** Crypto profile spent ~$0.78 today (256 AI calls) with
zero trades executed. Ensemble log: "ENSEMBLE HOLD at 0% confidence
across the board" for nearly every cycle. Specialists were ABSTAIN-ing
or returning generic HOLDs because crypto has none of the data they're
designed to read.

**Root cause:** Three of the four specialists need data sources that
don't exist for crypto:
- `earnings_analyst` — crypto has no earnings calls or filings
- `sentiment_narrative` — political/insider/options-flow inputs are
  equity-specific
- `risk_assessor` — portfolio concentration / Form 4 / SEC context
  doesn't apply

Running them produced noise that drowned out the one specialist
(`pattern_recognizer`) that can genuinely read crypto price action.

**Fix:** `ensemble.APPLICABLE_SPECIALISTS_BY_MARKET["crypto"] = {"pattern_recognizer"}`.
On crypto, only pattern_recognizer runs. Equity markets keep the full
4-specialist ensemble.

**Expected impact:**
- Crypto cost drops ~75% (1 specialist × chunks instead of 4)
- Pattern-recognizer's BUY/SELL verdicts now drive consensus directly
  (no dilution from ABSTAIN-ing peers)
- Crypto should start actually trading

**Tests:** `test_ensemble.TestSpecialistMarketApplicability` — 2 tests:
crypto-only-pattern, and equity-runs-all-four.

**Test count:** 419 (was 417 + 2).

---

## 2026-04-14 — Re-entry cooldown + skip political_context on crypto

**Severity:** medium (trade quality + cost efficiency)

**Bug: Position churn on same-symbol re-entry (ASTS)**
- 17:32 BUY ASTS @ $88.25 → 17:56 trailing stop triggered, sold @ $89.44
  (+$1.83 profit) → **18:02 BUY ASTS again @ $89.78** (6 min later,
  $0.34 higher than the exit). AI prompt had no "we just stopped out
  of this" context, so it re-selected ASTS as a high-conviction setup
  seconds after the protective exit fired.
- **Fix:**
  - New `recently_exited_symbols` table in per-profile DB
  - `journal.record_exit()` is called by `_task_check_exits` for every
    trailing-stop / stop-loss / take-profit firing
  - `trade_pipeline` pre-filter drops non-held symbols that appear in
    `get_recently_exited(cooldown_minutes=60)`. Held positions can
    still be managed (trimmed/exited); only fresh BUY entries are blocked.
- **Tests:** `test_reentry_cooldown.py` — 6 tests covering insert,
  expiry window, dedup on replace, missing-table safety, and the
  pipeline-filter contract.

**Cost optimization: Skip political_context on crypto**
- `political_sentiment.get_maga_mode_context` runs once per cycle when
  MAGA mode is on. It's ~$0.02 per call, equity-focused (tariffs,
  sector impacts). Crypto profiles called it ~40× today ($0.15/day
  wasted — crypto is macro-driven, not political-narrative-driven).
- **Fix:** `trade_pipeline.py` Step 4 skips the political context
  fetch when `ctx.segment == "crypto"`.
- **Expected impact:** Crypto AI cost drops ~20% per day.

**Open follow-up:** Small Cap / Crypto are still showing 0 trades.
Logs reveal the AI sees unanimous ensemble SELL conviction but passes
citing "sideways market regime". Not a bug — an AI decision pattern.
Separate task (#107) to decide whether to tune prompt to respect
strong ensemble consensus or accept cautious behavior during bootstrap.

**Test count:** 417 (was 411 + 6).

---

## 2026-04-14 — Systematic "insufficient data = N/A" pass across every metric

**Severity:** medium (UX correctness, not data integrity)

**Symptoms:** User audited the Performance Dashboard and found misleading
`0.00` values everywhere. Sharpe showing 0.00 with 1 day of data, Calmar
showing absurd numbers, Alpha/Beta showing 0.000 with insufficient data,
VaR showing 0.0 with no trades, Profit Factor showing 0.00 when there
are no wins, Current Streak showing "0 none" with no trades, etc. User
rightly pushed back: "I tell you to evaluate each page and you fix them
one at a time reactively."

**Root cause:** Widespread anti-pattern. Every `X if Y > 0 else 0.0`
collapses "undefined" and "zero" into the same display value. Users
can't distinguish "no data yet" from "your system produces no return."

**Fix:** Introduced a consistent `{metric}_computable` boolean alongside
every numeric metric that can be undefined. Template checks the flag
and renders **N/A** with a short "need X" hint instead of `0.00`.

**Metrics covered** (all now flag-guarded):
- `sharpe_ratio` — need ≥ 2 daily returns with positive std
- `sortino_ratio` — need ≥ 2 losing days
- `annualized_volatility` — same as Sharpe
- `calmar_ratio` — need ≥ 1% DD + ≥ 30 days
- `var_95` — need ≥ 5 closed trades
- `cvar_95` — same
- `win_rate` — need ≥ 1 closed trade
- `profit_factor` — need at least one win AND one loss
- `win_loss_ratio` — same
- `monthly_win_rate` — need ≥ 1 month of activity
- `alpha` — need ≥ 20 days aligned vs SPY
- `beta_spy` — same
- `correlation_spy / _qqq / _btc` — need ≥ 10 aligned days
- `slippage_vs_gross` — need positive gross profit
- `current_streak` — need ≥ 1 closed trade

**Tests:** `test_insufficient_data_guards.py` — 14 tests covering:
1. Every flag is emitted (not silently missing from the dict)
2. Empty data → all flags False
3. One-trade scenario (matches production state) → most flags False,
   ones that should compute (win_rate, streaks) return correctly
4. Sufficient data (30 snapshots, 5+ trades, wins+losses) → flags True

This is a **contract test**: a future refactor that removes a flag will
fail immediately with a pointed error message. Same mechanism we used
for the snake_case leak audit.

**Test count:** 411 (was 397 + 14).

---

## 2026-04-14 — Win/Loss Ratio shows undefined when ratio isn't computable

**Severity:** low (UX correctness — same class as the Calmar guard)

**Bug 8:** Win/Loss Ratio displayed `0.00` when the account had no
winning trades. The math `avg_win / abs(avg_loss) = 0 / X = 0.0` is
technically correct but misleads users into thinking they have a 0×
edge. The correct signal is "undefined — not enough data yet."

**Fix:** `metrics.py` emits `win_loss_ratio_computable = False` when
either `winning_trades` or `losing_trades` is empty. Template shows
**"N/A"** with a "need at least one win and one loss" hint instead
of `0.00`.

**Test:** `test_metrics_bugs.TestWinLossRatio` — three cases: no
wins, no losses, and both present (computes normal 2.0 ratio).

**Test count:** 397 (was 394 + 3).

---

## 2026-04-14 — Trade Analytics audit: 2 more bugs

**Severity:** medium (metrics display)

**Bug 6 — Avg Hold Days always 0.0**
- `metrics.py:765` matched buy→sell pairs by iterating the `trades`
  variable, which is the pnl-filtered list. Buys never have pnl set
  until the sell closes them, so BUY rows weren't in the list. Every
  SELL looked at an empty `open_positions` dict and recorded nothing.
- **Fix:** separate SQL query that fetches ALL trades (unfiltered) for
  the hold-days calculation. Buy/sell matching now works correctly.
- **Test:** `test_metrics_bugs.TestAvgHoldDays` — verifies a 04-13 buy
  + 04-14 sell yields 1.0 days, and empty-list case stays 0.0.

**Bug 7 — PnL distribution chart rendered same label 3× on single-bar charts**
- `metrics.render_bar_chart_svg:366` picked label indices `[0, len//2,
  len-1]` without deduping. A 1-bar chart collapsed all three to idx=0
  and rendered the label 3 times. User saw "-8% / -8% / -8%" when there
  was actually one trade bucketed to -8%.
- **Fix:** `sorted(set(...))` to dedup the idx list before rendering.
- **Test:** `test_metrics_bugs.TestSingleBarChartLabels` — 1 bar renders
  label 1×; 10 bars render 3 distinct labels.

**Test count:** 394 (was 389 + 5 new).

---

## 2026-04-14 — Executive Summary audit: 5 distinct bugs

**Severity:** medium (metrics wrong / misleading, not data-destructive)

**Symptoms:** User reviewed the Performance Dashboard's Executive Summary
tab and noted "a lot of 0s" despite a full day of trading. Audit revealed
5 distinct issues with how metrics are computed or displayed.

**Bug 1 — SELL trade with realized PnL stored as `status='open'`**
- `trade_pipeline.py:405` called `log_trade(pnl=pnl, ...)` on position
  closes without passing `status`. `journal.log_trade` defaults status
  to `'open'`. Result: closed positions with realized PnL appeared as
  open in the DB; downstream status-filter queries were wrong.
- **Fix:** pass `status="closed"` when pnl is not None on the sell path.
- **Test:** `test_metrics_bugs.TestSellStatusClosed`.

**Bug 2 — `daily_pnl` column always NULL**
- `_task_daily_snapshot` never passed `daily_pnl` to `log_daily_snapshot`.
  The column existed in the schema but had zero write paths.
- **Fix:** task now reads the most recent prior snapshot and stores
  `daily_pnl = today_equity - prior_equity`. First-ever snapshot stays
  NULL (no prior to compare against).
- **Test:** `test_metrics_bugs.TestDailyPnlPopulated`.

**Bug 3 — Calmar ratio produced absurd values with tiny drawdown**
- `metrics.py:585` divided annualized return by max_dd_pct with no floor.
  With 1 day of data and a 0.07% DD, Calmar became -310. That's
  mathematically correct but practically meaningless.
- **Fix:** require `max_dd_pct >= 1.0` AND `days_active >= 30` before
  computing Calmar. Below that, return 0.0 — the "insufficient data"
  sentinel already used elsewhere.
- **Test:** `test_metrics_bugs.TestCalmarGuard` with tiny-DD,
  insufficient-days, and meaningful-data scenarios.

**Bug 4 — Daily snapshot triggered only in a 5-minute window**
- `multi_scheduler.py:1221` gated snapshot on `now.hour == 15 and
  now.minute >= 55`. If the scheduler was restarted or paused through
  those 5 minutes, no snapshot that day. Two profiles were missing
  their 2026-04-12 snapshot because of this.
- **Fix:** trigger is now `now >= 15:55` for any time that day, with
  dedup via `last_run["daily_snapshot"]` date string. Missed-at-close
  is still caught later.
- **Test:** `test_metrics_bugs.TestSnapshotTriggerWindow` — both the
  trigger semantics and the dedup-by-date-string assertion (reads
  source to guarantee the dedup form isn't regressed).

**Bug 5 — Total Trades count excluded open positions**
- `metrics._gather_trades` filters `WHERE pnl IS NOT NULL`, so open
  positions never counted. A user who had made 3 trades (2 opens + 1
  close) saw "Total Trades: 1" and thought nothing had happened.
- **Fix:** added `_count_open_trades`; metrics dict now has
  `closed_trades`, `open_trades`, and `all_trades` (plus backward-compat
  `total_trades = closed_trades`). Template displays "3 (1 closed · 2
  open)". Win rate / profit factor / Sharpe still use closed trades
  only (those are the only trades with realized PnL to measure).
- **Test:** `test_metrics_bugs.TestTradeCountsIncludeOpen`.

**Follow-up:** one stray row on the server (Mid Cap LUNR sell) still
has status='open' from before the fix. Retroactively updated with a
one-line SQL on deploy. Future sells will get status='closed' correctly
via the code path.

**Total:** 11 new tests in `test_metrics_bugs.py`. Suite now 389 passing.

---

## 2026-04-14 — Risk specialist over-vetoing, earnings specialist noise-voting

**Severity:** high (trading completely blocked despite unanimous sell signals)

**Symptoms:** First live cycles after the tool_use fix showed ensemble was
producing real verdicts (previously all ABSTAIN), but trading was still
blocked. Per-cycle breakdown:
- `risk_assessor`: VETOing 53-80% of candidates (8/15 Mid Cap, 12/15 Small Cap)
- `earnings_analyst`: returning HOLD @ low confidence for 15/15 in every cycle
- Pattern + sentiment producing real signals but being drowned out
- Final AI correctly reasoning "mixed consensus" → pass

**Root cause:** Both specialists lacked meaningful per-symbol data in their
prompts (only symbol + signal + one-line reason). When asked to judge
without data:
- `risk_assessor` treated its "BIAS TOWARD CAUTION" + "VETO is final" as
  license to VETO anything ambiguous, including "sideways regime" and
  "low volatility" — which should be HOLD, not VETO
- `earnings_analyst` was explicitly instructed to "return HOLD with low
  confidence" when it had no earnings data — so it did, for every symbol,
  every cycle. That filled the consensus with neutral-but-valid HOLD votes
  that drowned out real signal

**Why it wasn't caught:** End-to-end trading behavior couldn't be tested
without running against a live Anthropic model. Unit tests of the ensemble
aggregation use mocked verdicts and don't reveal systemic miscalibration
in the prompts themselves.

**Fix:**
- `risk_assessor` prompt now explicitly lists INVALID VETO reasons
  ("uncertain market", "sideways regime", "low volatility", "general
  caution", "lack of information") — these are HOLD, not VETO. Also added
  a soft sanity check: "if you find yourself writing more than 2 VETOs in
  a batch of 5, re-examine". Removed the "BIAS TOWARD CAUTION" framing.
- `earnings_analyst` prompt now says: **omit symbols you can't assess**.
  Previously it returned HOLD for unknown symbols, polluting consensus.
  Now silence is the correct answer — only return verdicts for symbols
  with specific earnings/filing evidence (upcoming earnings date, recent
  surprise, SEC alert, etc.)

**Tests:** ensemble unit tests unchanged (mock-based, don't cover this).
Live validation required — watch next cycles for VETO rate < 20% on
risk_assessor and earnings_analyst producing verdicts for only a subset
of candidates (not 15/15 HOLD).

**Follow-up:** richer data in the specialist prompts (actual portfolio
state for risk, earnings calendar hits for earnings analyst) would let
them make informed verdicts instead of defaulting to safe-but-useless
output. Tracked informally as a design improvement.

---

## 2026-04-14 — Specialist ensemble silently abstaining on every call

**Severity:** critical (bordering on catastrophic)

**Symptoms:** Over 24 hours of live trading, Mid Cap profile made 2 trades,
Crypto made 0, Small Cap made 0. All 4 specialists showed `ENSEMBLE HOLD @
0% confidence` for every candidate. Final-decision AI correctly refused to
trade because "specialists universally abstain." No SHORT trades ever
executed despite STRONG_SELL technicals.

**Root cause:** Two compounding failures, both rooted in Haiku non-compliance:

1. **Shape failure** — Anthropic Haiku returns a single JSON object `{...}`
   instead of an array `[{...}, {...}]` for specialist prompts. The parser
   strictly required `isinstance(parsed, list)` and dropped the response
   when it wasn't.
2. **Drop failure** — Even with shape coerced, Haiku only returned 1-2 of
   15 requested candidates per call. The remaining 13 abstained by default,
   so the ensemble consensus was ABSTAIN/HOLD for almost every symbol,
   and the final AI refused to trade.

**Why it wasn't caught:** Unit tests mocked the AI call with clean JSON
arrays, never exercised the single-object branch or the truncated-response
branch. No integration test ran real specialist prompts against a real
provider.

**Fix** (three layers — only the third fully resolves the issue):

1. **Parser hardening** — `extract_verdict_array` now accepts: array,
   single object (wrapped), multiple concatenated objects, any of the
   above embedded in prose. Verified live — Haiku's single-object
   responses are now parsed correctly.
2. **Prompt strengthening** — all 4 specialist prompts now say "STRICT
   JSON ARRAY — starts with `[` and ends with `]`" and "You MUST return
   exactly {N} entries". Helped but not sufficient — Haiku still dropped
   candidates at size 15.
3. **Chunking + `tool_use`** — ensemble now chunks candidates into
   groups of 5 AND uses Anthropic's structured-output mode
   (`call_ai_structured` in `ai_providers.py`) to force schema
   compliance via a tool definition. **This is the fix that actually
   works.** Live probe verified 8/8 coverage per specialist (was 0-2/8).

**Cost impact:** With chunking + tool_use, ensemble is now 4 specialists ×
ceil(15/5) = 12 AI calls per cycle (was 4). Cost per cycle increases ~3×
but the ensemble now produces usable verdicts, which is the whole point.

**Tests added** (`test_ensemble.py`):
- `test_accepts_single_object_not_wrapped_in_array` — shape coercion
- `test_accepts_multiple_concatenated_objects` — streaming-object variant
- `test_accepts_object_with_surrounding_prose` — prose-wrapped variant
- `test_cost_scales_with_chunks_not_candidate_count` — chunking math
- `test_single_chunk_when_few_candidates` — small-shortlist sanity

**Gaps acknowledged:** No test uses a real Anthropic SDK to verify
tool_use works end-to-end. I ran a live probe on the server post-deploy
to confirm (8/8 verdicts returned). A mocked SDK integration test
covering the tool_use path would be valuable follow-up.

---

## 2026-04-14 — Snake_case leaking to AI Cost dashboard

**Severity:** medium (UX)

**Symptoms:** AI Cost panel showed `political_context`, `batch_select`,
`ensemble:risk_assessor`, etc., directly in user-facing tables — raw
internal identifiers instead of human labels.

**Root cause:** The `test_every_new_strategy_has_display_name` test was
scoped only to `STRATEGY_MODULES`. The `purpose=` tags emitted by
`call_ai` across 8 modules were never checked. Template also missed
the `| display_name` filter on the purpose column.

**Why it wasn't caught:** Existing test only validated strategy names.
No sweep across all identifier sources in the codebase.

**Fix:**
- Added 11 new `_DISPLAY_NAMES` entries covering every `purpose=` tag
- Added namespaced-fallback: `display_name("ensemble:foo_bar")` → `"Ensemble — Foo Bar"`
- Applied `| display_name` in the AI Cost panel template

**Tests added** (`test_display_names.py::TestNoSnakeCaseLeaksAnywhere`):
- `test_every_purpose_tag_has_human_label` — grep-discovers every
  `purpose=` literal in the codebase and asserts the rendered label has
  no underscores and is capitalized. Auto-catches any future tag.
- `test_known_purpose_labels` — exact assertions for 6 user-facing labels
- `test_namespaced_fallback_for_unknown_specialist` — future specialists
  pretty-print even without an explicit entry

---

## 2026-04-14 — `sync.sh` wiped live dashboard state on every deploy

**Severity:** high

**Symptoms:** Dashboard "AI Brain" panel showed "Waiting for first cycle..."
for Mid Cap and Small Cap profiles despite a full day of trading activity
recorded in their DBs. Multi-day breakage spanning ~6 deploys.

**Root cause:** `sync.sh` uses `rsync --delete` to mirror source → server.
Excludes were set for `*.db`, `*.pkl`, `.env`, `logs/`, `exports/` — but
`cycle_data_*.json` and `scheduler_status.json` were missing from the
excludes. Those files are written at runtime to the project root by
`trade_pipeline._save_cycle_data`. Every deploy wiped them. Crypto
regenerated quickly (24/7 cycle); equities only run during US market
hours, so their files stayed missing all evening.

Data itself was safe — per-profile DBs were correctly excluded.

**Why it wasn't caught:** The sync script has no self-test. I rewrote
it during the templates-flatten incident and didn't enumerate all
runtime files.

**Fix:**
- Added `--exclude 'cycle_data_*.json'` and `--exclude 'scheduler_status.json'`
  to `sync.sh`
- New `recover_cycle_data.py` one-shot script rebuilds missing cycle files
  from recent `ai_predictions` rows
- Freshness check in recovery script prevents overwriting live cycle data
  (`--force` flag for explicit override)

**Tests added** (`test_recover_cycle_data.py`):
- `TestSyncShExclusions::test_sync_excludes_runtime_artifacts` — reads
  `sync.sh` and asserts both exclusions are present. Fails with a message
  that points back at this incident if anyone removes them.
- 5 tests covering the recovery script (valid reconstruction, freshness
  check, force flag, missing-DB safety, empty-DB safety)

---

## 2026-04-14 — Capital allocator hardcoded `DEFAULT_WEIGHT = 1/6`

**Severity:** medium (latent — would have broken silently as library grew)

**Symptoms:** None yet — caught pre-production while expanding the
strategy library from 6 → 16. With the hardcode, 16 new strategies each
got a "default" weight of 1/6 = 16.67% = 2.67× oversized. Normalization
would still sum to 1.0 but relative weights between no-track-record
strategies would be wrong.

**Root cause:** `multi_strategy.DEFAULT_WEIGHT = 1.0 / 6` was a module-level
constant hardcoded to the original library size.

**Fix:**
- Replaced with `_default_weight(n_strategies)` function computed per-call
  using the actual `len(strategy_names)` from the current allocation

**Tests added** (`test_today_integration.py`):
- `test_default_weight_scales_inversely_with_count` — validates at 6, 16, 40
- `test_one_hot_strategy_capped_redistributed` — cap-and-redistribute math
  at 16-strategy library size
- `test_three_hot_strategies_all_capped` — edge case where multiple
  strategies hit the 40% cap

---

## 2026-04-14 — `sync.sh` flattened `templates/`, wiped running web UI

**Severity:** critical (production web UI broke, 500 errors)

**Symptoms:** `GET /login` returned HTTP 500 after a routine deploy.
Flask couldn't find `templates/` anywhere.

**Root cause:** The prior `sync.sh` passed multiple directory arguments
to rsync (`templates/`, `static/`, `strategies/`, `tests/`) — each with
a trailing slash. rsync's semantics for `<src>/` with multiple sources
merges all their *contents* into the target root, so `templates/base.html`
and `strategies/__init__.py` both landed at `/opt/quantopsai/` root.
`--delete` then removed the actual `templates/` directory because it
was no longer "in source" after the flattening.

**Why it wasn't caught:** No deploy-smoke test. The sync script wasn't
tested.

**Fix:**
- Rewrote `sync.sh` to sync the project root as a single source
  (`/Users/mackr0/Quantops/` with trailing slash → `/opt/quantopsai/`),
  preserving directory structure
- Deploy restored templates/ and put everything back in correct subdirectories
- `deploy.sh` updated to explicitly include `strategies/` and `tests/`

**Tests added:** Indirectly by the cycle_data guardrail test, which also
asserts other critical exclusions are present. A dedicated deploy-smoke
test would be better — tracked informally as a hygiene follow-up.

---

## Pre-changelog fixes (retroactive — limited context)

Entries before this date were not tracked contemporaneously. Reconstructed
from session memory; details may be incomplete.

### 2026-04-13 — Capital allocator cap redistribution infinite-excess bug

**Severity:** high

**Symptoms:** At a single strategy, the 40% cap logic capped it to 40%
and had "nowhere to redistribute" the 60% excess, so that capital was
simply lost from the allocation (sum < 1.0). At 2 strategies with both
over-cap, the redistribution oscillated and left sum < 1.0.

**Root cause:** Original cap loop used a stale snapshot of `normalized.items()`
and redistributed excess based on a single pass that didn't iterate to
convergence.

**Fix:** Iterative cap-and-redistribute loop in `multi_strategy.compute_capital_allocations`.
Stops when no strategy is over the cap or no strategies are under the cap.
Single-strategy case keeps 100% (nowhere to redistribute; correct behavior).

**Tests:** `test_multi_strategy.TestCapitalAllocations::test_weights_always_sum_to_one`
covers 1, 2, 6 strategies. `test_no_strategy_exceeds_forty_percent_cap`.

### 2026-04-13 — Statistical significance assertion using numpy booleans

**Severity:** low (test-only)

**Symptoms:** Rigorous backtest test failed with `np.True_ is True`
mismatch on assertion.

**Root cause:** `scipy.stats` returns numpy booleans, not Python `bool`.
`assert result["significant"] is True` fails even when the test is
semantically correct.

**Fix:** Wrapped return values with `bool()` in `rigorous_backtest.py`.

### 2026-04-13 — `/api/portfolio/{id}` passing profile dict instead of id

**Severity:** medium

**Symptoms:** API endpoint returned errors instead of portfolio data.

**Root cause:** `build_user_context_from_profile()` expects profile_id,
was being called with the profile dict itself.

**Fix:** Pass `prof["id"]` instead of `prof`.

### 2026-04-12 — Stop/target displayed as raw percentages ($0.13, $0.19)

**Severity:** medium (UX + correctness)

**Symptoms:** Trades showed stop-loss as $0.13 and take-profit as $0.19
— these were 13% and 19% values stored as raw percentages but rendered
as dollar prices.

**Root cause:** `execute_trade` stored `stop_loss_pct` directly rather
than converting to a dollar price at the time of trade.

**Fix:** `stop_price = price * (1 - actual_sl_pct)` at execution.
Retroactively fixed existing trade rows in the DB.

### 2026-04-12 — Total return +199.8% on "All Profiles" view

**Severity:** medium (correctness)

**Symptoms:** Dashboard showed impossibly high aggregate returns when
"All Profiles" was selected.

**Root cause:** `_gather_snapshots()` summed per-day snapshots across
profiles without forward-filling gaps. A profile missing a day's
snapshot contributed zero, distorting the aggregate.

**Fix:** Forward-fill missing days per profile before aggregation.

### 2026-04-12 — Tab persistence lost on profile dropdown change

**Severity:** low (UX)

**Symptoms:** Changing the profile dropdown lost the active tab hash
(e.g., `#ai` → bare URL).

**Root cause:** Form submit replaced `window.location` without preserving
`.hash`.

**Fix:** Inline `onchange` handler that captures `window.location.hash`
and re-appends before submit.

---

## How to add a new entry

When fixing a production bug, copy this template:

```markdown
## YYYY-MM-DD — Short title

**Severity:** critical | high | medium | low

**Symptoms:** What the user/operator saw.

**Root cause:** What was actually wrong in the code.

**Why it wasn't caught:** Honest answer — missing test coverage,
wrong assumption, etc.

**Fix:** What changed. Point at files.

**Tests added:** Named tests in `test_*.py` that prevent regression.
If none exist yet, track it as a follow-up TODO.

**Follow-up (optional):** Related work not done in this fix.
```

Add the entry **before the deploy ships**, not after. Severity is
assessed on impact, not how hard the fix was.

