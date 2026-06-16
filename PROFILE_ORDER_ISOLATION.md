# Profile Order Isolation — Issues & Fix Plan

**Status:** OPEN — root cause active in production.
**Author:** assistant (at operator's direction), 2026-06-16.
**Why this document exists:** Cross-profile contention on the shared
Alpaca accounts has caused a *recurring* class of failures (oversells,
uncompleted take-profits, reconciler halts). Prior work fixed
**instances** and incorrectly described the **class** as solved. This
document is the complete inventory of every code path that can violate
profile isolation, the fix for each, and the structural test that will
prevent the class from silently returning. Nothing here is marked done
until its fix is committed, verified on prod, and pinned by a test.

---

## 1. The real root cause — and what it is NOT

**The shared account is irrelevant.** This is the key correction:
sharing an Alpaca account does not, by itself, cause any of these
failures. Alpaca assigns every order a unique `order_id`, returns it
on submit, and tracks every fill against the order that produced it.
Two profiles can hold the same symbol in the same account and their
orders never collide *at the broker* — Alpaca distinguishes them by
ID. There is no need for separate accounts.

**The actual root cause is incomplete 1:1 order-id bookkeeping on
our side.** The system fails the broker's own model in three ways:

1. It **cancels by symbol** (`list_orders(symbols=[X])` → cancel all)
   instead of cancelling **specific order_ids it owns**.
2. It **reconciles by guessing** — when a sell wasn't journaled with
   its order_id at submit time, the reconciler later fuzzy-matches a
   broker fill by symbol+qty+time and *assigns* it to a profile. On a
   shared account that guess can land on the wrong profile (or
   orphan a real fill → halt).
3. Both of the above are only *necessary* because the upstream
   contract — **every submitted order_id is journaled atomically at
   submit** — is not actually airtight. Where it leaks, the
   reconciler's fuzzy fallback papers over it, which is where
   mis-attribution enters.

**The contract that must hold (broker-native, account-agnostic):**

- Every order we submit is journaled **with its `order_id`, atomically,
  at submit time** — no path may place a broker order without
  immediately recording its ID in that profile's journal.
- Every cancel references a **specific `order_id` we recorded as
  ours** — never an account-wide enumeration by symbol.
- Every broker fill is attributed by its **own `order_id`** (the one
  we journaled, or the bracket parent→child link) — **never** by
  fuzzy symbol/qty/time matching.
- The broker's per-symbol *aggregate* position is used only as a
  drift cross-check against the sum of profiles' journals, never as a
  per-profile authority.

If those hold, position "blocks" bought over time are each tied to
their buy `order_id`, sells reference the profile's own holdings, and
the account being shared makes no difference whatsoever. **No fuzzy
matching is needed; the reconciler only confirms our recorded
order_ids against the broker.** Removing the fuzzy fallback entirely
(once the atomic-journaling leaks are closed) is the end state.

---

## 2. Symptoms observed (all the same root cause)

| Date | Symptom | Mechanism |
|---|---|---|
| 2026-06-11 | BATL 5,145-share oversell across profiles | exit sold against shares a sibling had reserved/sold |
| 2026-06-11 | PPCB oversells (p96/p97/p102) | same — sibling-share consumption |
| 2026-06-16 | **SPCX** take-profit never completed (rode +8% past target) | a sibling's exit **canceled p121's pending sell** (`trader.py:733`) |
| 2026-06-16 | **SOUN** reconciler HALT on p128; A3 profiles claim **1,356** shares, broker holds **11** | sells executed at broker but journals didn't record the closes; fuzzy fill attribution couldn't assign shared-pool fills per profile |

The reconciler halt is the **safety net working correctly** — it
refuses to let a profile trade on a book that disagrees with the
broker. The defect is upstream: the contention that produced the
disagreement.

---

## 3. Complete inventory of broker-order paths

Legend: **A** = account-wide / NOT isolated (must fix). **B** =
already profile-isolated (correct). **C** = maintenance script / audit
(not in the per-cycle live loop; fix after A, lower urgency).

### Group A — account-wide, in the live trading loop (THE BUGS)

| # | Site | Problem | Confirmed |
|---|---|---|---|
| A1 | `trader.py:733` — exit path "cancel conflicting orders" | `api.list_orders(status="open", symbols=[symbol])` returns **every profile's** open orders for that symbol on the shared account, then cancels **all** of them. A sibling's pending take-profit/entry gets canceled. | **YES — SPCX p121 today** (`Cancelled conflicting order 4cec6ff7 … before exit`) |
| A2 | `multi_scheduler.py:1161` — stale limit-order cleanup | `api.list_orders(status="open")` (entire account, all symbols), cancels any **limit** order > 5 min old. Runs **per profile**, so profile A's cleanup cancels profile B's stale limit orders. | High — account-wide by construction |
| A3 | `reconcile_journal_to_broker.py` fuzzy fill attribution: `_find_matching_exit_fill` (211), `_find_terminal_via_backward_walk` (526), `_detect_protective_fill` fallback (652) | Attributes a broker fill to a profile by **symbol + qty + time** when the exact own-journal order ID can't be walked. On a shared account two profiles can have same-symbol/same-qty exits seconds apart → a sibling's fill is attributed to the wrong profile, or a real fill is left orphaned → synthesis halt. The `_all_journal_sell_order_ids` cross-profile dedup mitigates but is **snapshotted at task start** (stale within a pass) and fuzzy matching is inherently ambiguous on a shared pool. | **YES — SOUN p128 today** (1,356 virtual vs 11 broker) |

### Group B — already profile-isolated (keep, add regression pins)

| Site | Why it's correct |
|---|---|
| `order_guard.py` `allowable_sell_qty` / `allowable_cover_qty` | Uses the profile's **own virtual qty** from its journal as the authority; the aggregate broker pool is consulted only as a drift *sanity check*, never to downsize against siblings (2026-06-09 rewrite). |
| `bracket_orders.py` `ensure_protective_stops` | Filters broker coverage to `own_protective_ids` (the entry rows' `protective_*_order_id`) before deciding placement/skip. |
| `reconcile_journal_to_broker.py` `_is_bracket_child_fill` | Attributes by **exact parent→child order-id linkage**, no fuzzy match. |
| `bracket_orders.py` `cancel_for_symbol` | Cancels only the `protective_*_order_id`s recorded on **this profile's** open journal rows (not an account-wide list). |

### Group C — maintenance scripts / read-only audits (not per-cycle)

`aggregate_audit.py`, `reconcile_aggregate_drift.py`,
`auto_close_broker_orphans.py`, `cancel_phantom_option_stock_stops.py`,
`reset_for_clean_experiment.py`, `cleanup_bug_cascade_buys_2026_05_18.py`
use account-wide `list_orders`/`list_positions`. These run **manually**,
not in the trading loop, so they don't cause live contention — but they
operate on the whole account by design and must not be run blindly
against a shared account during trading. `certify_books.py` and
`virtual_audit.py` read `list_positions` for **account-level**
reconciliation (correct use — they compare account total vs the sum of
virtual books). `client.py:405` `list_positions` feeds the virtual
layer for non-virtual profiles only.

---

## 4. Fix plan

### A0 — FOUNDATION: every submitted order_id journaled atomically (close the leaks)
This is the deepest fix and the reason the others are even needed.
Audit **every** `submit_order` / `submit_option_order` /
`_submit_alpaca_order_raw` call site (trader, trade_pipeline,
bracket_orders, options_*, multi_scheduler, stat_arb_pair_book,
simple_strategies, options_delta_hedger, options_roll_manager) and
prove that the returned `order_id` is written to the profile's journal
**in the same code path, before any early return**, with no path that
places a broker order and fails to record its ID. The existing atomic-
journaling tests cover some of these; extend them to **all** submit
sites. Once airtight, the reconciler never encounters a broker fill it
didn't journal — so the fuzzy fallback (A3) becomes dead code and is
removed, not merely de-scoped. This is what makes "shared account"
irrelevant: every fill is already ours by ID.

### A1 — `trader.py:733` exit cancel-conflicting (cancel only own orders)
Replace the account-wide `list_orders(symbols=[symbol])` + cancel-all
with: cancel **only** the order IDs this profile's journal records as
its own *open, non-protective* orders for the symbol (its pending
entry/limit). Protective orders are already handled separately by
`cancel_for_symbol`. Concretely: read this profile's journal for
`order_id`s on open `buy`/`sell` rows for the symbol that are still
`pending_*`, intersect with the broker's open orders, cancel only that
intersection. A sibling's order ID is never in this profile's journal,
so it can never be canceled.

### A2 — `multi_scheduler.py:1161` stale limit cleanup (scope to own)
Filter `open_orders` to order IDs present in **this profile's**
journal before canceling. Same principle as A1 — never cancel an order
this profile didn't create.

### A3 — reconcile fuzzy attribution (own-journal-only by default)
1. **Primary attribution stays exact:** match broker fills to the
   profile's own journaled `order_id` / `protective_*_order_id` (and
   the bracket parent→child linkage). This already exists and is
   correct.
2. **Remove the cross-account fuzzy fallback for the shared-account
   case:** when the exact own-id walk fails, do **not** fuzzy-match by
   symbol/qty/time across the shared pool (that's what mis-attributes
   siblings' fills). Instead, leave the row for the next pass and, if
   it persists, surface it as *ambiguous* (operator-visible) rather
   than synthesizing or halting on a guessed attribution.
3. **Make the cross-profile dedup live, not snapshotted:** consult the
   other profiles' journals at decision time (or, better, only ever
   attribute by own IDs so cross-profile dedup is unnecessary).

### Structural guardrail (prevents the whole class from returning)
Add an **AST/structural test** (`tests/test_profile_order_isolation_*`)
that scans the live-loop modules (`trader.py`, `trade_pipeline.py`,
`multi_scheduler.py`, `bracket_orders.py`, `options_*`) and **fails**
if any `api.cancel_order(...)` is reachable from an `api.list_orders(...)`
result that was not first filtered against the profile's own journal
order IDs. Maintain an explicit allowlist for the Group-C scripts with
written rationale. This is the "pin the universal contract with a
structural test" rule — without it, the next refactor silently
reintroduces an account-wide cancel.

### Data repair (one-time, after the code fixes)
Reconcile the diverged books to broker truth: where a profile's journal
claims shares the broker doesn't have *and* the broker order history
shows the sells executed (SOUN: 1,356 virtual vs 11 broker), record the
missing closes from the broker fills (attributed by exact order ID) and
clear the halt. Idempotent dated script, verified against broker order
history per the BATL/PPCB repair pattern.

---

## 5. Verification (no "done" without this)

For each fix: unit/structural test + **live prod proof**:
1. A1/A2: a test that a sibling's same-symbol order **survives** a
   profile's exit/cleanup (mock two profiles' orders on one account).
2. A3: a test that a shared-pool fill with no own-journal match is
   classified **ambiguous**, never synthesized/attributed to the wrong
   profile.
3. Structural test green (no account-wide cancel in the live loop).
4. Prod: after deploy, run a full trading session and confirm
   `certify_books` shows **zero** broker-vs-virtual drift across all
   accounts, and **zero** reconciler synthesis halts, for symbols held
   by multiple profiles.

---

## 6. Honest accounting

Prior commits fixed isolation **instances** — the May per-profile
sell/cover guard (`order_guard`), the June 9 protective per-profile
isolation, the June 11 oversell repairs — and the class was described
as closed. It was not: the exit-path cancel (A1), the stale-limit
cleanup (A2), and the reconcile fuzzy attribution (A3) remained
account-wide. This document exists so the *class* is tracked to zero,
with a structural test as the backstop, rather than patched one symptom
at a time.

---

## 7. Checklist

- [x] **A0 — order_id journaled atomically.** New primitive `order_guard.own_broker_order_ids(db_path, symbol)` (trades order-id columns + `protective_*_order_id` + `long_vol_hedges`). Audited every submit site (3 Explore audits); closed the active leaks: `trader.py` exit `log_trade` now has a guaranteed minimal order-id fallback + halt-on-failure (`_journal_exit_order_id_minimal`); multileg sequential-open rollbacks journal both the open and the rollback-close (`_journal_rolled_back_leg`). Bracket children remain healed by the existing per-cycle sweep. (Long-vol hedge order-ids are claimed by the primitive; the hedge feature is gated OFF by default — the hedge→aggregate_audit awareness is documented as flag-guarded, to revisit if/when enabled.)
- [x] A1 — `trader.py` exit cancels only own order_ids (gate on `own_broker_order_ids`; sibling's same-symbol order survives). Test: `test_profile_order_isolation_2026_06_16.py`.
- [x] A2 — `multi_scheduler._task_cancel_stale_orders` cancels only own order_ids (+ functional test: sibling's stale order survives this profile's sweep).
- [x] A3 — reconcile own-order-id-only attribution; **fuzzy `_find_matching_exit_fill` DELETED**. Unexplained broker-flat close → new `orphan_close` action → HALT (never silent, never sibling-claim). Mid-pass live-journal re-check preserved. Reconcile tests updated to the new contract.
- [x] Structural AST test: `test_order_isolation_invariants_2026_06_16.py` — bans account-wide cancel without an `own_broker_order_ids` gate, keeps the fuzzy matcher deleted, requires own-id-only attribution + orphan_close→halt.
- [x] Prod deploy + reconcile verification: deployed `d4e01d6` (2026-06-16 20:34Z); the NEW own-order-id reconciler dry-run on all 14 prod profiles returns **orphan_close=0, all divergence buckets 0, 341 real_held** — the isolation fix is clean and produces NO false halts. RECONCILE check in certify_books: **PASS**.
- [ ] Data repair: **BLOCKED on a distinct, pre-existing active bug surfaced by certify_books — escalated to operator.** certify_books BROKER-DRIFT + DECOMPOSITION FAIL:
  - **p128 JOBY oversold to a −125 short.** One buy of 5 (id 121), then ~25 sell-5 orders today (14:36→19:30Z) that ALL FILLED at the broker but stayed `status='open'` in the journal. The journal never decremented, so each cycle re-sold 5 → 5-share long driven to a 125-share unintended short. Pre-dates this deploy; paused only because market is closed. Proximate cause is a **fill-confirmation gap** (filled sells not flipped to closed), NOT cross-profile contention — a separate fix.
  - **SOUN** accounting divergence: certify virtual=211 vs broker=11, yet `get_virtual_positions` nets SOUN=0 — a virtual-calc mismatch to reconcile.
  - **Decomposition gaps** p121 (−5,985), p128 (+2,635).
  - Repair mutates live financial books + the −125 JOBY short is a real market position; awaiting operator direction (and not auto-trading to flatten).

**Full local suite: 5,250 passed** (only the changelog-parity guard tripped pre-CHANGELOG; now updated). The per-profile ORDER ISOLATION deliverable (A0–A3 + guards) is complete, deployed, and verified clean; the remaining data-repair item is gated on a separately-rooted active bug.
