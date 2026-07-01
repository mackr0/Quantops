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
`P_win` = `meta_model.predict_probability` (cold-start: symbol reputation win-rate,
else conviction prior `clip(0.50 + 0.06·|score|, 0.50, 0.68)`).
`reward_net$ = reward$ − cost$`, `risk_net$ = risk$ + cost$`. This materializes the
stock's dollar max-loss for the first time, erasing the option's phantom "defined-risk" edge.

**Option spread (vertical):** premiums fetched on the prompt path → `_vertical_pl_bounds`
→ `max_loss/gain/contract`, `breakeven`. `qty = floor(REF$/max_loss_per_contract)` ≥1.
`risk$ = max_loss_per_contract·qty` (fallback `OptionStrategy.total_max_loss` = width×$100×qty
when the short-leg mark is untrusted — `value_parity` fuzziness), `reward$ = max_gain·qty`,
`cost$` = per-leg half-spread. `P_win` (POP) = **min** of (a) short-strike delta rule and
(b) breakeven-distance ÷ implied-move — the conservative lower.

Both land as the same dimensionless RAR. **Rank key** in `_rank_candidates`: replace
`abs(score)` with `RAR·(1−_div_penalty)` desc, tie-break EV$. All existing suppressors
(held-underlying, IV dead-zone, `_options_budget_exhausted`, short-quality, long/short
reservation) preserved. The prompt's "capital-efficient / lower max-loss" option thumb is deleted.

## Feedback loop (own-book only — never pooled across profiles)

1. **Veto shadow prediction** — a veto today is only a `broker_rejections` row, invisible to
   every win-rate query. Write a shadow `ai_predictions` row on veto so its would-be P&L
   resolves; discount option RAR by per-`(strategy × sector)` P(veto) **before** selection.
   Adapts within tens of cycles. (Shadow rows are pending predictions, NEVER orders — excluded
   from position/equity/order-id reconstruction; freshness + isolation invariants intact.)
2. **Realized-RAR shrinkage** (nightly) — pull modeled option P_win toward this profile's own
   realized option win-rate. Primary guard against POP optimism.
3. **Expression-aware meta-model** (P4) — add `pipeline_kind` one-hot + option-geometry features
   so stock-vs-spread of the same name get different P once ≥100 resolved rows accrue.

## Phased plan

- **P0** — fix `pred_type` (`trade_pipeline.py:2643-2661`): MULTILEG_OPEN/OPTIONS mislabeled as
  directional_long → corrupts every per-expression stat. Prerequisite, no behavior change.
- **P1** — price option recs (`evaluate_candidate_for_multileg` → premiums + `_vertical_pl_bounds`;
  fail-open to width×$100) + emit stock dollar risk$/reward$ (`stock_strategy_advisor`). Data only.
- **P2** — the RAR scorer (new risk_adjusted.py module) + independent streams in `_build_candidates_data`
  + flat-pool rank in `_rank_candidates` + single `render_opportunity_ledger` in `_build_batch_prompt`
  (delete the option thumb). Respects `enable_options` (p201 → stock-only ledger). The core.
- **P3** — veto shadow-prediction feedback (`pipelines/option._record_veto` + new veto_feedback.py module).
- **P4** — learned per-expression calibration (nightly realized-RAR rollup + `meta_model` features).

## Confirmed operator decisions (2026-07-01)

1. Risk shape: **risk-neutral EV** + conservative min-of-two POP; add a tail-penalty λ only if realized tails demand it.
2. Per-expression floor/cap: **none** — pure RAR decides; monitor.
3. Feedback aggressiveness: **partial blend**, ~30 resolved rows min, floored discount.
4. AI override: **default-with-reason** (not hard rank); log overrides to measure if they beat the number.
5. Common notional: **same capital-at-risk envelope** for stock and option ("same bet").
6. Premium fetch on the hot path: **yes — cached + fail-open** (skip the option row, keep the stock row on failure).

## Success metrics (per profile, on prod)

Option:stock ratio → stable RAR-driven mix (not re-skewed either way); option veto rate drops;
idle cash falls; **realized RAR of the two expressions converges toward parity** (proof the score
is calibrated, not biased); modeled-vs-realized POP gap monitored; entry-scan latency not regressed.
