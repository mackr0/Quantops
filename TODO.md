# Backlog — separate-session work

Items deferred from active session because they're additive features
or incremental refactors that don't fit in the current commit chain.

Each item names what to build, the user-visible problem it solves,
and any technical pre-requisites or pitfalls.

**Last reconciled against code: 2026-05-21.** Most of the 2026-05-11
incident backlog has shipped; see the Shipped section at the bottom.

---

## P0 — Phase 4B1: incremental fine-tuning (the headline next project)

**Full spec**: `docs/20_FINETUNE_PHASE_4B1_INCREMENTAL.md`
(Status there: SCOPING — not yet implemented.)

**What**: Fine-tune a custom model on this system's own trade history
so the apex LLM internalizes the candidate universe, regime tagger,
specialist taxonomy, and historical outcomes — instead of relying
purely on a frozen base model + RAG injection at inference.

**Why now**: the data foundation is complete as of 2026-05-21:
- B1 (#184) — archive-before-reset + prompt/response persistence +
  cycle history + cycle_id linkage. ✅ shipped 2026-05-19.
- B2 (#185) — multi-horizon outcomes table + deterministic-panel
  rule-vote snapshots + `ai_tracker.build_training_dataset()`.
  ✅ shipped 2026-05-21.
- B3 (#186) — cost-adjusted net returns. ✅ shipped 2026-05-21.

`build_training_dataset()` already emits per-prediction rows with
parsed features, rule_votes, prompt_text, raw_response, and
multi-horizon outcome labels (return_pct, return_pct_net, mfe/mae,
outcome_class). That's the training corpus.

**Open scoping questions** (resolve before building — see the
session scoping notes appended to docs/20):
1. Which collected signals belong IN the training input vs stay as
   ex-post meta-model features. (Tuning parameters, ticker activity,
   meta-model scores — see scoping discussion.)
2. Train target: next-action imitation, outcome-conditioned, or
   reward-weighted on return_pct_net?
3. Base model + method: LoRA on an open model, or a hosted
   fine-tune API? Cost economics already analyzed in #187.
4. Soak/shadow harness reuse — docs/20 already describes a
   per-profile shadow-model soak; confirm it covers the new model.

---

## P1 — Still open

### #6 — Opportunistic migration off the Position dict shim

**Status**: ONGOING. `position.py` still carries the back-compat
shim (`__getitem__` / `.get()` / `__contains__`). The guardrail
(`tests/test_no_new_position_dict_access.py`) blocks producer
regressions but consumers still use dict access.

**Scope**: each commit migrates ONE consumer file from
`pos["symbol"]` / `pos.get("qty")` to `pos.broker_symbol` /
`pos.qty_signed` / `pos.is_option`. When the last consumer is
migrated, delete the shim and the dict-access bug class becomes
impossible to construct. One file per commit (reviewable).

### #7 — Proactive exits for single-leg long options

**Status**: PARTIAL. `options_lifecycle.py` resolves options at
EXPIRY (marks closed, computes P&L). What's MISSING is proactive
exit before expiry for single-leg long calls/puts:
- Premium-based stop: close when current premium drops N% from
  entry (e.g. 50%), using the contract bid (not stock-style %).
- Time-based exit: close at N days-to-expiry (e.g. 7 DTE) to avoid
  gamma blowup.

Multileg legs are managed at the spread level (structural max
loss) — skip them. Submission is OCC-side
(`position_intent=sell_to_close`). Pitfall: option bid-ask spreads
are wide — prefer limit-at-mid with a market fallback.

---

## Methodology — class tests over instance tests

Mack flagged 2026-05-11: the suite grew mostly via *instance tests*
(one per case). The higher-leverage pattern is *class tests* — one
test that catches the entire bug shape via AST/regex scan,
property-based invariant, or table-driven roundtrip from a source
of truth.

**Rule**: before writing test #2 of a similar shape, ask "is there
an invariant that catches both #1 and #2 plus cases I haven't
thought of?" If yes, write the class test instead.

**Patterns to reach for first**:
1. AST / regex scan for code-shape bugs.
2. `hypothesis` property tests for invariants over input space.
3. Table-driven tests parametrized from a source of truth.
4. Roundtrip tests (every entry in mapping X roundtrips through Y).

---

## Process

- New items at the top of their priority section.
- When work starts, link the commit; on completion move to Shipped
  with the ship date.
- Don't expand an item into a multi-week saga without re-checking
  scope with Mack first.

---

## Shipped (reconciled 2026-05-21)

### P0 — Instrument-class pipeline architecture (all 6 phases)
`docs/14_INSTRUMENT_PIPELINE_ARCHITECTURE.md`. Verified present:
- `pipelines/` — ABC + `stock.py` / `option.py` / `registry.py` /
  `dispatch.py` / `specialist_router.py` / `option_prompt.py`
- `metrics/` — `stock.py` / `option.py` / `portfolio.py` / `legacy.py`
- `tuning/` — `stock.py` / `option.py`
- `pipelines/outcomes/` — `stock.py` / `option_resolver.py` /
  `recalibrate.py` / `backfill.py` (per-pipeline outcome resolution)
- Greek-aggregated exposure via `options_greeks_aggregator.py`

### P1 — UI gaps
- #1 Options vs stocks tabs — dashboard + /trades both split by
  `is_option` / `occ_symbol`.
- #2 Numbered pagination on /trades — `_build_page_links` in views.
- #3 Symbol search on /trades — `?search=` param, bound (no injection).
- #4 Action column shows `signal_type` badge in `_trades_table.html`.
- 4a Docs sweep — test counts + ±10% drift guardrail.
- 4b AI-pipeline option-handling audit → became the pipeline roadmap.

### P2 — 2026-05-11 incident follow-ups
- #5 broker_rejections surfaced in views + dashboard. (Note: confirm
  win-rate analytics EXCLUDE rejected predictions — display done,
  exclusion not separately verified.)
- #8 Slippage % option/stock scoping — `test_slippage_pct_kind_scoping`.
- The 23-phantom-stop incident — fully closed (broker cancel +
  ensure_protective_stops option-skip + Position class refactor).

### Multi-leg + reconciliation hardening (2026-05-20/21)
- #189 multileg journal OCC symbol bug.
- #192 per-key ensemble lock.
- #195 position cap soft bound + live same-cycle decrement.
- cover-as-cash-OUT equity fix; tainted-prompt tagging.
- Same-provider Gemini model fallback (lite → flash).
- Protective orders journal at placement (orphan-fill class closed).
- Protective protection decided by Alpaca truth (order_id-keyed) +
  `verify_protective_order_sync` invariant.
- Buy-qty guard median computed from stock buys only.
