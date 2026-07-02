# Risk-Adjusted Expression Selection — Design (approved 2026-07-01)

Replaces the asymmetric stock-vs-option presentation (which drove an ~18:1
option:stock proposal skew → concentration vetoes → idle cash) with a single
**risk-adjusted opportunity ledger**. The stock expression and each option-spread
expression of every candidate are generated as **independent opportunities**,
scored on **one axis**, ranked together, and handed to the AI as one ledger. The
AI stays the final chooser (override-with-reason; the number is the default).
A healthy mix is the *output* of ranking — nothing is favored.

## Scoring model (apples-to-apples, both expressions)

```
EV$  = P_win · reward_net$ − (1 − P_win) · risk_net$
RAR  = EV$ / risk_net$          # expected profit per dollar at risk, cost-netted
```

Both expressions sized to the **same capital-at-risk envelope** `REF$ = size_pct · equity`
(size_pct = conviction-scaled max_position_pct; equity = get_account_info(ctx)).

**Stock (BUY/SHORT):** `risk$ = REF$·(stop_loss_pct/100)`, `reward$ = REF$·(take_profit_pct/100)`
(ATR-clamped stop/TP from `stock_strategy_advisor`), `cost$` = round-trip slippage,
`P_win` = this profile's OWN realized SAME-DIRECTION win-rate (the pure directional
reputation bucket — BUY reads BUY, bearish reads SHORT; never the HOLD-dominated
symbol aggregate, never the mixed-exit SELL bucket), partial-blended toward the
conviction prior `clip(0.50 + 0.06·|score|, 0.50, 0.68)`; thin bucket → prior alone.
(A meta-model probability is a data-gated future refinement, not consulted today.)
`reward_net$ = reward$ − cost$`, `risk_net$ = risk$ + cost$`. This materializes the
stock's dollar max-loss for the first time, erasing the option's phantom "defined-risk" edge.

**Option spread (vertical):** premiums fetched on the prompt path → `_vertical_pl_bounds`
→ `max_loss/gain/contract`, `breakeven`. `qty = floor(REF$/max_loss_per_contract)` ≥1.
`risk$ = max_loss_per_contract·qty` (fallback `OptionStrategy.total_max_loss` = width×$100×qty
when the short-leg mark is untrusted — `value_parity` fuzziness), `reward$ = max_gain·qty`,
`cost$` = per-leg half-spread — the REAL live-quote `(ask−bid)/2` per leg, round-trip
(derived from the same one-snapshot-per-leg fetch that prices the leg), falling back
to a conservative fixed per-leg cost only when a two-sided market isn't quotable. `P_win` (POP) = **min** of (a) short-strike delta rule and
(b) breakeven-distance ÷ implied-move — the conservative lower.

Both land as the same dimensionless RAR. **Rank key** in `_rank_candidates`: replace
`abs(score)` with reputation-aware RAR desc — the concentration haircut `(1−_div_penalty)`
applied to POSITIVE scores only (a negative RAR is never made to look better by
concentration) — tie-broken on |score| then RSI-extremity; the ledger's own sort
tie-breaks on EV$. All existing suppressors
(held-underlying, IV dead-zone, `_options_budget_exhausted`, short-quality, long/short
reservation) preserved. The prompt's "capital-efficient / lower max-loss" option thumb is deleted.

## Feedback loop (own-book only — never pooled across profiles)

1. **Veto-rate discount** — a veto today is only a `broker_rejections` row, invisible to every
   win-rate query. Record every option proposal's outcome (vetoed/accepted) keyed by
   `(strategy × sector)` in a dedicated `option_proposal_outcomes` table and discount option RAR
   by that P(veto) **before** selection. Adapts within tens of cycles.
   **Architecture note (2026-07-01, operator mandate "perfect data, zero contamination"):** the
   outcomes live in their OWN table, PHYSICALLY SEPARATE from `ai_predictions` — NOT a shadow
   `ai_predictions` row behind an exclusion filter. Physical separation is the strongest possible
   guarantee that a would-be/veto outcome can never leak into real-trade reputation / meta-model /
   win-rate stats (no reader can forget a filter it never needed). The would-be-P&L *resolution*
   of vetoed spreads (to learn whether the vetoes were smart) is folded into P4, where the resolver
   + its consumption live; the table's nullable resolution columns are ready for it.
2. **Realized-RAR shrinkage** (nightly) — pull modeled option P_win toward this profile's own
   realized option win-rate. Primary guard against POP optimism.
3. **Expression-aware meta-model** (P4) — `option_open` added to the existing
   `prediction_type` one-hot (shipped), plus data-gated option-geometry features later,
   so stock-vs-spread of the same name get different P once ≥100 resolved rows accrue.

## Status (2026-07-01)

P0–P4 are ALL SHIPPED (P2a scorer + P2b ledger/ranking + P3 veto-rate discount
+ P4 would-be-P&L resolver, veto-quality calibration, and the `option_open`
meta-model one-hot). The full risk-adjusted selection engine + its self-learning
veto-feedback loop are live. Remaining refinements are data-gated (they need
weeks of resolved option outcomes to matter): option-geometry meta-model
features, and pulling modeled option POP toward realized option win-rate.

## Phased plan

- **P0** — fix `pred_type` (`trade_pipeline.py:2643-2661`): MULTILEG_OPEN/OPTIONS mislabeled as
  directional_long → corrupts every per-expression stat. Prerequisite, no behavior change.
- **P1** — price option recs (`evaluate_candidate_for_multileg` → premiums + `_vertical_pl_bounds`;
  fail-open to width×$100) + emit stock dollar risk$/reward$ (`stock_strategy_advisor`). Data only.
- **P2** — the RAR scorer (new risk_adjusted.py module) + independent streams in `_build_candidates_data`
  + flat-pool rank in `_rank_candidates` + single `render_opportunity_ledger` in `_build_batch_prompt`
  (delete the option thumb). Respects `enable_options` (p201 → stock-only ledger). The core.
- **P3** — per-(strategy × sector) veto-rate discount: `option_proposal_outcomes` table (own-book,
  separate from ai_predictions) written at the option pipeline's veto + accept sites; `veto_feedback.py`
  computes the P(veto) discount (≥30 samples, capped 0.5, positive-RAR-only); applied in the ledger.
- **P4** — would-be-P&L resolver (prices vetoed spreads at veto time, resolves them intrinsically at
  expiry on the resolve cadence) + veto-QUALITY calibration (`discount = P(veto) × loss-fraction`, so
  only strategies whose vetoes actually avoided losses are down-ranked) + `option_open` one-hot in
  `meta_model` so the GBM can calibrate stock vs spread separately. All in `option_proposal_outcomes`
  (own-book, separate from ai_predictions). Data-gated refinements (geometry features, POP shrinkage) noted.

## Confirmed operator decisions (2026-07-01)

1. Risk shape: **risk-neutral EV** + conservative min-of-two POP; add a tail-penalty λ only if realized tails demand it.
2. Per-expression floor/cap: **none** — pure RAR decides; monitor.
3. Feedback aggressiveness: **partial blend**, ~30 resolved rows min, floored discount.
4. AI override: **default-with-reason** (not hard rank); log overrides to measure if they beat the number.
   *(IMPLEMENTED 2026-07-01: `opportunity_ledger.tag_overrides` flags trades that took a lower-RAR expression than the ledger's best; per-cycle count logged in `ai_select_trades`; metadata persisted on the prediction; `override_scorecard(db_path)` compares realized override-vs-aligned outcomes.)*
5. Common notional: **same capital-at-risk envelope** for stock and option ("same bet").
6. Premium fetch on the hot path: **yes — cached + fail-open** (skip the option row, keep the stock row on failure).

## Success metrics (per profile, on prod)

Option:stock ratio → stable RAR-driven mix (not re-skewed either way); option veto rate drops;
idle cash falls; **realized RAR of the two expressions converges toward parity** (proof the score
is calibrated, not biased); modeled-vs-realized POP gap monitored; entry-scan latency not regressed.
