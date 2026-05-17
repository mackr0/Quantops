# QuantOpsAI Experiment Design — Post-Audit Fresh Start (2026-05-17)

## Why this document exists

After the multi-day zero-error audit (see CHANGELOG batches 1-9), the
journal/cash/drift machinery was found to have several latent bugs that
caused the previous experiment's results to be unreliable: stock short
proceeds weren't credited to virtual cash, option contract multipliers
were missing in cash math, multileg combo writes corrupted per-leg
prices, drift between broker and journal accumulated silently.

All those bugs are fixed (3291 tests pinning behavior). Before any
real money is deployed, this document defines the FRESH experiment
that will tell us:

1. **Does the system add value over null benchmarks?**
2. **Which components are pulling weight?** (ablations)
3. **Is a $25K real-money deployment ready?** (the specific
   capital level we will trial first)
4. **Are the $25K results reproducible or noise?** (replicas)
5. **Does the strategy survive at 10× and 40× scale?**

---

## Architecture reminder

- **3 Alpaca paper accounts** = real broker accounts the system uses
  for execution + funding. NOT the unit of accounting for the user.
- **N virtual QuantOps profiles** = the actual units of accounting.
  Each = one strategy with its own initial_capital, AI config, risk
  params, journal, P&L. Many profiles share one Alpaca account.
- **The journal is truth.** Dashboard equity is computed from
  `journal.get_virtual_account_info()`, NOT from broker API.

---

## The 13-profile experiment

### Account 1 — Benchmarks (~$1M paper)

Three profiles competing on equal capital ($333K each). Direct
apples-to-apples comparison of "AI vs null hypotheses."

| Profile | Capital | What it does | Purpose |
|---|---|---|---|
| **Buy & Hold SPY** | $333,000 | Buys SPY on day 1, holds; rebalances weekly only if SPY weight drifts >5% | Null floor. If full system doesn't beat this, the product is worthless. |
| **Random Stock-of-Day** | $333,000 | Each day picks 5 random stocks from S&P 500, holds 1 week, repeats. Zero AI involvement. | Second null. Tests whether the AI's stock-picking adds value above random selection. |
| **Full System** | $333,000 | All AI features ON (alt-data, meta-model, specialists, self-tuning, options, sentiment, regime). Same universe as the other two for fair comparison. | The best shot the system has. Must beat both nulls to justify the product. |

**Win condition for Acct 1**: Full System > Random > Buy & Hold by
margin > monte-carlo noise floor at 6 months.

### Account 2 — Ablations (~$800K paper)

Same starting capital ($200K each), same universe, same risk params.
One component disabled per profile so you can attribute alpha to
specific components.

| Profile | Capital | Disabled | Reveals |
|---|---|---|---|
| **No Alt-Data** | $200,000 | Insider / Congress / Form4 / 13F / sentiment feeds; AI only sees price + technicals | Alpha contribution of paid/scraped data |
| **No Meta-Model** | $200,000 | GBM + SGD adjustment layer; raw AI confidence used directly | Alpha contribution of the ML calibration layer |
| **No Self-Tuning** | $200,000 | Self-tuner frozen at initial params; no auto-adjustment | Alpha contribution of feedback learning |
| **No Options** | $200,000 | Single-leg + multi-leg options disabled; stocks only | Alpha contribution of the options pipeline |

**Win condition for Acct 2**: Each ablation should underperform the
Full System (Acct 1) by a measurable amount. If "No Alt-Data" equals
the Full System after 6 months, alt-data adds nothing and should be
removed.

### Account 3 — Product candidate + capital scaling (~$1.55M paper)

The actual investment question. Six profiles all running the SAME
configuration (best of all strategies, constrained to $25K-feasible
instruments) at different capital levels and replications.

| Profile | Capital | Purpose |
|---|---|---|
| **$25K Candidate** | $25,000 | THE investment decision. Decides whether real money gets deployed. |
| **$25K Replica A** | $25,000 | Reproducibility test #1 — different RNG, same config |
| **$25K Replica B** | $25,000 | Reproducibility test #2 |
| **$250K (10×)** | $250,000 | Does the same strategy work at 10× scale? Tests slippage model + position-sizing math |
| **$1M (40×)** | $1,000,000 | Does it scale to fund-style capital? Tests concentration / liquidity caps |
| _(unassigned)_ | ~$250K | Slack — not deployed to any profile |

**$25K Candidate constraints (best-of-all-strategies CONSTRAINED to
what works at small capital):**
- `max_total_positions = 5` (concentration over diversification at this size)
- `max_position_pct = 0.20` (up to 20% per position; allows conviction)
- `enable_short_selling = False` (shorts tie up too much margin at $25K)
- Options: single-leg only (multi-leg spread margin makes them
  infeasible at $25K)
- All AI features ON (alt-data, meta-model, specialists, self-tuning)
- Universe: liquid mid-to-large caps (no illiquid microcaps where
  $25K is the whole float)

**Win condition for Acct 3**:
- $25K Candidate: positive return after costs, Sharpe > 1.0, max
  drawdown < 15% over 6 months
- Replicas A + B: within ±5% of Candidate's result (proves it's
  signal not luck)
- 10× and 40× scaling profiles: returns degrade no more than 30%
  vs $25K Candidate (acceptable slippage cost of scale)

If all three of those win conditions hit → real $25K deployed.

---

## Implementation status (2026-05-17)

| Arm | Status | What's missing |
|---|---|---|
| Buy & Hold SPY | 🟡 Column exists | `strategy_type='buy_hold'` column added 2026-05-17; dispatch code (~150 LOC in trade_pipeline strategy router) pending batch B. |
| Random Stock-of-Day | 🟡 Column exists | `strategy_type='random'` column added 2026-05-17; dispatch code pending batch B. |
| Full System | ✅ Default | Works with current code (all flags ON). |
| No Alt-Data | ✅ Implemented | `enable_alt_data` column added + gate in `trade_pipeline._get_universe_context` (2026-05-17). |
| No Meta-Model | ✅ Implemented | `enable_meta_model` column added + gates in `_meta_pregate_candidates` and main meta-model load (2026-05-17). |
| No Self-Tuning | ✅ Exists | `enable_self_tuning = 0` already supported. |
| No Options | ✅ Implemented | `enable_options` column added + gate in `ai_analyst.build_prompt` multileg_block (2026-05-17). |
| No Shorts | ✅ Exists | `enable_short_selling = 0` already supported. |
| $25K Candidate (constrained) | ✅ Exists | All needed knobs (`max_total_positions`, `max_position_pct`, `enable_short_selling`, `initial_capital`) already supported. |
| $25K Replicas | ✅ Exists | Same config × N profiles works today. |
| Capital-scaling profiles | ✅ Exists | `initial_capital = 250000 / 1000000` already supported. |

**9 of 11 arms work today (batch A complete 2026-05-17). 2 still
require strategy_type dispatch code (batch B). Estimated effort:
~1 day.**

---

## Decision needed before launching the experiment

Three options:

1. **Build the missing 5 arms first** (1-2 days), launch the full
   13-profile design. Most defensible scientifically.

2. **Launch with what works today** (the 6 ✅ arms): $25K Candidate
   + 2 replicas + 3 capital-scaling + No-self-tuning + No-shorts as
   the only ablations. Loses the SPY-baseline / random / alt-data /
   meta-model / options ablations. Faster to start, weaker science.

3. **Hybrid**: Launch the 6 ✅ arms now, build the missing 5 in
   parallel and add them as new profiles once ready (~week 2 of
   experiment). Acceptable if the missing arms are "nice to have"
   ablations; less so if you need the SPY baseline from day 1 to
   measure relative alpha.

---

## What gets reset / kept on launch

**Reset:**
- All per-profile journal tables: `trades`, `ai_predictions`,
  `virtual_profile_state`, `ai_cost_ledger`, `activity_log`
- (Optional, with `--wipe-ai-memory`): `specialist_outcomes`,
  `tuning_history`, `learned_patterns`, `meta_model_state`,
  `strategy_validations`, `ai_shadow_calls`
- All open Alpaca broker positions (close via `--close-broker`) so
  the experiment doesn't inherit legacy positions

**Kept:**
- Profile configs in `quantopsai.db` (trading_profiles,
  alpaca_accounts, users) — though we may want to CREATE NEW
  profiles with the experiment design above rather than re-using
  the current 10
- Altdata DBs (insider, congresstrades, edgar13f, edgar_form4,
  biotechevents, stocktwits) — these are world data
- Code, tests, deployment infra

---

## Success criteria summary

After 6 months of paper trading on this design:

| Outcome | Action |
|---|---|
| Full System beats Buy-Hold SPY AND beats Random by Sharpe > 1.0 | System validated — ablation results tell us what to keep |
| $25K Candidate + 2 replicas all positive with σ < 5% | Deploy real $25K |
| $25K Candidate fails OR replicas wildly diverge | Don't deploy. Diagnose. |
| Capital-scaling profiles degrade > 30% | Strategy doesn't scale; cap real deployment at the largest size that still works |
| Any ablation profile matches Full System | That component isn't contributing alpha — remove from prod to reduce cost/complexity |

---

## Open implementation work

Tracked as follow-up tasks in the QuantOps task list:

- **Batch B (next)**: Build `strategy_type='buy_hold'` and
  `strategy_type='random'` dispatch in trade_pipeline so profiles
  with those strategy_type values run their bespoke logic instead
  of the AI pipeline.
- **Batch C**: Clean wipe of orphaned per-profile DBs left over
  from the old Alpaca accounts that were deleted 2026-05-17, then
  rebuild fresh profiles per this experiment design.
- **Dashboard**: Comparative-returns chart (#164) — overlay every
  profile's daily equity curve against the SPY and Random
  baselines so relative alpha is visible at a glance.
- (Done 2026-05-17, batch A) `enable_alt_data` / `enable_meta_model`
  / `enable_options` / `strategy_type` columns + ablation gates.
- (Done) Reset script `reset_for_clean_experiment.py` — ready to
  run when launch design is finalized.
- (Done) Perfect-matching invariant: every trade row carries the
  broker order_id; warning fires if not (#157).
