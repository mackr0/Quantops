# Backlog — separate-session work

Items deferred from active session because they're additive features
or incremental refactors that don't fit in the current commit chain.

Each item names what to build, the user-visible problem it solves,
and any technical pre-requisites or pitfalls.

---

## P0 — Active risk (none currently)

(All P0 items as of 2026-05-11 have been shipped. The 23-phantom-
stock-stops incident is fully closed: canceled at the broker,
recurrence prevented by ensure_protective_stops option-skip guard,
root cause fixed by Position class refactor.)

---

## P1 — User-visible UI gaps

### 1. Options vs stocks as separate tabs

**Where**: dashboard Open Positions section + /trades page.

**Why**: option positions and stock positions have fundamentally
different fields and economics. Today they share a single table
(`_trades_table.html`) and the macro has to conditionally render
OPT badges, contract-detail subtext, x100 multipliers, per-spread
P&L, etc. Tabs would split them cleanly and let each table show
exactly the fields that matter for its instrument type. Should
follow the same tab pattern as the performance page.

**Scope**:
- Dashboard `templates/dashboard.html`: add Tabs ("Stocks" |
  "Options") inside the Open Positions block per profile. Split
  positions list at the view layer using `pos.is_option`.
- /trades `templates/trades.html`: top-level tabs ("Stock Trades"
  | "Option Trades"). Server-side filter on `signal_type` /
  `occ_symbol`.
- Probably split the macro into `_stock_trades_table.html` and
  `_option_trades_table.html` once they diverge enough — currently
  the shared macro has a lot of `{% if is_option %}` branches.
- Option-specific columns: strike, expiry, days-to-expiry, premium
  per contract, total premium (x100), per-spread P&L grouping.
- Stock-specific columns: shares, share price, total cost, current
  share price, unrealized P&L.

**Prerequisite**: nothing — `pos.is_option` already lands via the
Position class refactor (Phase 1, shipped 2026-05-11).

**Pitfalls**: don't lose the unified "all trades" view entirely
— some operators may want one mixed table for audit purposes.
Consider keeping a "All" tab.

---

### 2. Server-driven page-jump pagination on /trades

**Where**: `templates/trades.html` + `views.py:trades()`.

**Why**: /trades has grown to ~20 pages of history. Current UI only
has prev/next arrow buttons. Reaching page 18 requires 17 clicks.
Need numbered page links with jump-to-page support.

**Scope**:
- `views.py:trades()` already does server-side pagination (50/page).
  No data-layer change needed.
- `templates/trades.html`: replace the prev/next-only pagination
  block with a numbered page bar like:
  `« 1 2 3 ... 10 11 [12] 13 14 ... 19 20 »`
  with active page highlighted. Show ~5 pages around the current
  page; clip with ellipses on either side.
- Page links must preserve all other query parameters
  (`profile_id`, `sort`, `dir`, future `search`).
- Optional: a small "Jump to page" input box (Enter to navigate).

**Pitfalls**: when a sort/filter changes, current page may become
invalid; the existing route already clamps `page` to `total_pages`,
keep that.

---

### 3. Symbol search on /trades

**Where**: `templates/trades.html` + `views.py:trades()` +
`_get_trade_history_for_profile`.

**Why**: with ~20 pages of trades, finding "every CWAN trade"
requires scrolling. A search box gives instant filter-by-symbol.

**Scope**:
- Add `?search=<query>` URL parameter. Treat as case-insensitive
  prefix on `symbol` AND substring on `occ_symbol` (so "CWAN"
  matches both stock and option rows for CWAN).
- `_get_trade_history_for_profile(profile_id, limit=200,
  search=None)` — extend signature; build `WHERE` clause that
  filters when `search` is non-empty.
- `templates/trades.html`: search input on the same form as the
  profile dropdown; submits a GET with the search query.
- Pagination links preserve the search parameter alongside sort.

**Pitfalls**: SQL injection — bind the search as a parameter, never
interpolate. Empty/whitespace search should be treated as "no
filter" so the URL doesn't double-encode.

---

### 4. Expand "Side" column to show the actual action type

**Where**: `_trades_table.html` macro.

**Why**: Today the column shows just `BUY` or `SELL`. The journal
stores more detail in `signal_type` (`BUY`, `STRONG_BUY`,
`WEAK_BUY`, `SELL`, `STRONG_SELL`, `WEAK_SELL`, `SHORT`, `COVER`,
`MULTILEG_OPEN`, `PAIR_OPEN`, `PAIR_CLOSE`, `OPTIONS`, `OPTION_EXERCISE`,
`DELTA_HEDGE`). Showing the signal_type tells the operator
*what kind* of trade it was — STRONG_BUY conviction vs WEAK_BUY
hedge, opening vs closing leg, etc.

**Scope**:
- Macro reads `t.signal_type` and renders as a small uppercase
  badge with appropriate color (BUY/STRONG_BUY → green, SELL →
  red, MULTILEG_OPEN → purple, etc.). `display_names.humanize`
  for human-readable form.
- Rename column header from "Side" to "Action".
- Keep the existing BUY/SELL coloring for the row's `pnl-pos`/
  `pnl-neg` class on side context.

**Pitfalls**: signal_type values are inconsistent across older
trade rows. Defensive: fall back to `t.side` (uppercased) when
signal_type is missing.

---

### 4b. Audit the AI pipeline for option-specific handling end-to-end

**Where**: every step from "AI proposes a trade" → "broker submission"
→ "position tracking" → "exit logic" → "outcome resolution".

**Why**: today's incident response surfaced six bugs where option
positions were treated like stocks (the symbol-vs-OCC overload).
Those were the visible ones. The same root mindset — "options were
bolted onto a stock-first system" — may have produced other
silent issues in: the AI prompt (does the AI know what option
strategies to propose? what features about IV/Greeks does it see?),
the signal pipeline (do strategies emit option-friendly signals
or just stock signals reused?), the AI prediction tracker (does it
resolve option outcomes correctly — strike, expiry, premium decay?),
the metrics layer (does Sharpe / win-rate count options correctly?
do contracts vs shares net out in qty-weighted stats?).

**Scope**:
- Walk each stage and document option-specific behavior vs
  borrowed-from-stock. Output: a doc listing every "options use the
  stock path here" finding + whether it's a bug, an acceptable
  reuse, or a TODO.
- For each finding tagged as a bug, file a ticket with the same
  Position-class principle: option-aware code paths use
  `pos.is_option` / `pos.broker_symbol` / spread economics.
- Pay particular attention to:
  - `ai_analyst.py` prompt construction — what features does the AI
    see about an option candidate? IV rank? Greeks? Days to expiry?
    Spread economics?
  - `ai_tracker.resolve_predictions` — does an option win/loss
    resolve at expiry vs at exit? How is "return %" measured for
    a contract vs a share?
  - `metrics.py` Sharpe / Sortino / win-rate — do option contracts
    count once or x100? Are short option legs treated as positive
    or negative qty in turnover stats?
  - `self_tuning.py` parameter optimization — is it tuning stock
    parameters with option trades mixed in? That would corrupt the
    stock-tuner's signal.
  - Strategy signals — do strategies that propose `MULTILEG_OPEN`
    have option-specific feature sets, or are they just stock
    signals with a wrapper?
- Add a guardrail test or two for any structural invariants that
  surface (e.g., "if signal_type=MULTILEG, expected_features must
  include implied_vol_rank").

**Prerequisite**: Position class refactor in place (already shipped).
The audit becomes much easier because option-vs-stock is now
explicitly tagged on every position object.

**Pitfalls**: this audit could surface a LOT of work. Be aggressive
about classifying findings: not every "options use stock code here"
is a bug — some reuse is correct (e.g., AI provider call is the
same regardless of instrument). Don't make every reuse into a
ticket.

---

### 4a. ✅ DONE 2026-05-11 — Documentation sweep — test counts + drift

(commit pending — see CHANGELOG entry "TODO #4a: docs sweep — stale test counts updated + ±10% drift guardrail")

### 4a-archive. Documentation sweep — test counts + drift

**Where**: `docs/13_QUALITY_RELIABILITY.md` (QE/RE doc) and any
sibling doc that quotes test counts or post-incident state.

**Why**: docs quoting specific test counts (e.g., "2,234 tests")
drift every time a new test lands. Mack flagged 2026-05-11 that
the QE/RE doc's numbers are wrong relative to the current suite
(2,708 passing as of `9df7463`). More broadly: every doc that
cites a specific number, file path, or function signature is at
risk of going stale silently — the only reliable enforcement is
a guardrail test (we already have one for OPEN_ITEMS file:line
refs; the same pattern could cover docs).

**Scope**:
- Audit every `docs/*.md` for outdated test counts, broken file
  refs, stale architecture descriptions, references to deleted
  features or renamed functions.
- Update or strike each.
- Add a guardrail test: scan `docs/*.md` for patterns like
  `\d{3,4}\s+(?:tests?\s+)?(?:passing|pass\b)` and either (a)
  compare to the current suite count, OR (b) require the number
  to live in an auto-generated section so updates are mechanical.
  Similar pattern for file:line refs (we already have
  `test_open_items_refs_match_source.py`).

**Prerequisite**: nothing — docs are static; CI guardrail is
additive.

**Pitfalls**: don't tie docs too tightly to the test count or
every commit needs a doc bump. The guardrail should auto-replace
the number when CI runs, OR flag staleness rather than fail outright.

---

## P2 — Pending from 2026-05-11 incident

### 5. AI Brain panel surfaces broker_rejections inline

**Why**: today, broker-rejected trades are persisted in the
`broker_rejections` table (shipped fbd375c) but the AI Brain panel
still shows "TRADES SELECTED" with no execution outcome. Mack went
looking for CWAN BUY today because nothing on screen surfaced the
rejection.

**Scope**:
- View layer: query `broker_rejections` for recent rejections in
  the same window the AI Brain panel covers, indexed by
  `prediction_id` (when set) or `(symbol, action, timestamp)`.
- Macro/template: render a small badge on each TRADES SELECTED row:
  ✅ submitted / ❌ REJECTED (with broker reason in a tooltip) /
  ⏳ pending.
- Win-rate analytics path (`ai_tracker.resolve_predictions` etc.)
  must EXCLUDE predictions that have a matching broker_rejection
  row — they didn't actually trade, they shouldn't influence the
  AI's measured win rate.

**Prerequisite**: broker_rejections persistence + helpers (already
shipped fbd375c).

**Pitfalls**: prediction_id ↔ rejection linkage is currently NULL
in most rows (we didn't thread it through the rejection handler
yet). For now, match by (symbol, action, timestamp ±5min). Add
prediction_id thread-through later for tighter binding.

---

### 6. Phase 5b+ — opportunistic migration off Position dict shim

**Why**: the Position class refactor (2026-05-11) introduced a
back-compat shim (`__getitem__`, `.get()`, `__contains__`) so
existing consumers continued working without a single massive
migration. The guardrail (`tests/test_no_new_position_dict_access.py`)
blocks producer regressions but allows consumer dict access.

**Scope**: each cleanup commit migrates ONE consumer file from
`pos["symbol"]` / `pos.get("qty")` to `pos.broker_symbol` /
`pos.qty_signed` / `pos.is_option` etc. Files to migrate (rough
order of safety + impact):
  1. `views.py` (highest user-facing surface)
  2. `bracket_orders.py`
  3. `trader.py`
  4. `portfolio_manager.py`
  5. `trade_pipeline.py`
  6. `reconcile_journal_to_broker.py`
  7. ... everything else

When the last consumer is migrated, a final commit deletes the
`__getitem__` shim from `position.py` and the bug class becomes
literally impossible to construct.

**Prerequisite**: nothing — phase 1 produced the shim, every
incremental migration is independently shippable.

**Pitfalls**: don't migrate too many files in one commit (hard to
review). One file per commit, with the relevant tests still
passing.

---

### 8. Slippage stats showing impossible values (1130% avg)

**Where**: performance page Slippage Impact panel (and underlying
metrics calc).

**Why**: Mack flagged 2026-05-11 — performance page shows:
  - Avg Slippage: **1130.102%**
  - Net Slippage Cost: -$1,863.13
  - Execution Variance: $15,167.15
  - Trades with Fill Data: 1,004

A 1130% average slippage is impossible — slippage is bounded by the
bid-ask spread plus a small market-impact term, typically <0.5%
for stocks and a few % for liquid options. The two dollar columns
($1,863 cost vs $15,167 variance) also don't reconcile in a way
that makes economic sense.

**Hypothesis** (related to today's option-handling discoveries):
the slippage % is being computed as `(fill - decision) / decision`
on option premiums, which produces 10-100% moves on normal
contract value swings (a $0.01 → $1.00 mark-to-market on an OTM
option = 10,000% by that formula). The `1004 trades with fill data`
bucket likely includes option legs whose entry premium was
pennies — when the closing premium is anything more than pennies,
the % balloons. Probably the same option-vs-stock conflation
class that produced the phantom-stock-stops, the deferral-forever
bug, etc.

**Scope**:
- Find the slippage calc (likely `journal.get_slippage_stats`
  or `metrics.py`).
- Audit it for option vs stock handling. Per-leg option premium
  % is misleading; spread-level dollar is more meaningful.
- Either: (a) compute slippage in DOLLARS and report dollars-
  only on options, OR (b) compute % only when entry premium is
  above a sanity floor (e.g. $0.10) so penny premiums don't
  produce 10,000% spurious values, OR (c) split the panel into
  Stock Slippage / Option Slippage with appropriate units.
- Add a guardrail test that asserts displayed slippage % is
  bounded (e.g. < 200%) for any individual row.

**Prerequisite**: Position class refactor in place (already
shipped). Easier to detect option vs stock now that we have the
canonical attributes.

**Pitfalls**: don't just clip the display — the underlying calc is
wrong, and clipping hides the real distortion in net-slippage
aggregates. Fix at the calc layer.

---

### 7. Option-side exit logic for single-leg long options

**Why**: today, defined-risk multileg spreads are protected by
their structural max loss (debit paid). Single-leg long option
positions have no broker-side protective order — `ensure_protective_stops`
skips all options to avoid the phantom-stock-stops bug. So a long
call/put could lose 100% of premium with no automated exit.

**Scope**:
- Premium-based stop-loss: close the position when current
  premium drops by N% from entry (e.g., 50%). Uses the option
  contract's bid (not stock-style %).
- Time-based exit: close at N days to expiry (e.g., 7 DTE) to
  avoid gamma blowup. Already exists in `options_lifecycle.py`?
  Verify; extend if not.
- Submission must be OCC-side: `api.submit_order(symbol=OCC,
  qty=N, side="sell" if long else "buy", type="market" or "limit",
  position_intent="sell_to_close" or "buy_to_close")`.
- Skip multileg legs — they're managed at the spread level.

**Prerequisite**: nothing — Position class makes the OCC routing
unambiguous via `pos.broker_symbol`.

**Pitfalls**: option bid-ask spreads are wide; market orders can
fill badly. Prefer limit-at-mid with a fallback to market after
N seconds.

---

## Process

- New items added at the top of their priority section.
- Priority bumps: items move between sections as urgency changes.
- When work starts on an item, link the commit / branch in the
  item; on completion, move it to a "Shipped" section with the
  ship date.
- Don't expand an item into a multi-week saga without re-checking
  scope with Mack first.
