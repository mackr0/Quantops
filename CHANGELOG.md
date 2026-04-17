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
