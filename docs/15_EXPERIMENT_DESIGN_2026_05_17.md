# QuantOpsAI Experiment Design v2 — Post-Audit Fresh Start (2026-05-17)

## Why this document exists

After the multi-day zero-error audit (CHANGELOG batches 1-12) and the
subsequent visibility-to-action build-out (#165–#171), the journal,
broker reconciliation, integrity-audit, and self-tuner machinery are
all in known-good state. Before any real money is deployed, this
document defines the FRESH experiment that will tell us five things:

1. **Does the system beat null benchmarks?** — Is there any AI alpha at all?
2. **Which components are pulling weight?** — Are alt-data / meta-model / self-tuning / options each adding measurable value, or is one of them a cost center?
3. **Are those components complementary or redundant?** — Does removing two at once lose more than the sum of removing them individually?
4. **Is a $25K real-money deployment ready?** — Specifically: does the constrained best-of-all-strategies config produce positive return + Sharpe > 1.0 + max drawdown < 15% over 6 months?
5. **Does the strategy scale, and would lifting constraints unlock alpha?** — Conservative-scaling test (10×) AND an aggressive-freedom variant that drops the small-account constraints.

This v2 design replaces the v1 plan (the original 13-profile table earlier in this file's git history). v2 was rewritten 2026-05-17 to fix five real data-quality problems with v1: mixed ablation/baseline capital sizes confounding ablation deltas; only one Random null (high variance); the $1M (40×) profile asked the same question as the $25K Candidate just bigger; no combined-ablation arm to detect component interactions; and no upside-aggressive control to bound whether the conservative default leaves alpha on the table.

---

## Architecture reminder

- **3 Alpaca paper accounts** = real broker accounts the system uses for execution + funding. NOT the unit of accounting for the user.
- **N virtual QuantOps profiles** = the actual units of accounting. Each = one strategy with its own initial_capital, AI config, risk params, journal, P&L. Many profiles share one Alpaca account.
- **The journal is truth.** Dashboard equity is computed from `journal.get_virtual_account_info()`, NOT from broker API.
- **The seven-tier integrity contract** (order_id pairing, qty parity, value parity, cash parity, basis parity, equity identity, reconciler heartbeat — all in `aggregate_audit.py` and `integrity_audit.py`) runs every 10 minutes via `audit_runner.detect_and_alert_new_drift`. First detection of any new drift emails the operator. This means the experiment's measurements are trustworthy — any leak between broker and journal surfaces within minutes.

---

## The strategy types in play

Each profile is assigned a `strategy_type` (column on `trading_profiles`, default `'ai'`). Three values are valid. The experiment uses all three:

### `strategy_type='ai'` — the full pipeline

The default mode. Runs the complete trade pipeline: screener → ensemble of specialists → meta-model calibration → AI analyst (the LLM call that picks the winning trades and writes the rationale) → execution gates → order submission.

**Components active by default:**
- **Alt-data feeds**: insider buys (Form 4), Congressional trades, 13F changes, sentiment (StockTwits), biotech catalysts (PDUFA / AdComm), patents, app store rankings
- **Meta-model**: GBM + SGD calibration layer that adjusts raw AI confidence based on the profile's per-symbol track record
- **Specialists**: pattern recognizer, risk assessor, options strategy advisor, etc. — each casts a vote that the AI sees in the prompt
- **Self-tuner**: 12-layer autonomous parameter adjustment that runs daily and adjusts confidence thresholds, position sizes, strategy weights, etc. based on the profile's resolved-prediction track record
- **Options**: single-leg AND multi-leg options recommendations included in the AI prompt
- **Sentiment**: per-symbol sentiment scores from StockTwits message volume
- **Regime detection**: market regime (bull / bear / chop) drives different position sizing
- **Crisis monitor**: cross-asset doomsday signals trigger position-size reductions

Each of these can be turned off individually via the corresponding `enable_X` column. The ablation profiles in Account 2 use exactly this mechanism to test marginal contribution.

### `strategy_type='buy_hold'` — null floor

Spends ~95% of equity on SPY on day one and holds. Only re-trades when SPY weight drifts more than 5% from 100% (covers accumulated cash from dividends). Never sells voluntarily. Zero AI involvement, zero alt-data, zero ML.

**Why it's in the experiment:** This is the simplest possible "do nothing" strategy. If the full system can't beat this over 6 months of paper trading, the product is worthless and we should refund the user's time. Every claim the AI makes has to clear this floor first.

### `strategy_type='random'` — random-stock-of-day null

Each market day deterministically picks 5 symbols at random from the curated large-cap universe (seed = `hash((profile_id, today_iso))` so a re-run on the same day picks the same 5 — no churn). Closes any position not in today's pick, opens new picks equal-weighted from available cash. Zero AI involvement.

**Why it's in the experiment:** Tests whether the AI's *stock-picking* adds value over chance. Buy-Hold-SPY is too easy to beat in a strong-bull regime (the AI just has to pick anything in the index). Random-stock-of-day strips out the index-membership advantage — it's a stricter "is your stock selection actually good?" test.

**Why TWO Random replicas (A and B):** Random is by definition high-variance. A single Random profile could be unlucky for 6 months and look terrible, or lucky and look great. With two replicas using different `profile_id` (different RNG seeds), we get a tight bound on Random's variance band — the comparison to AI becomes statistically meaningful instead of "Full System beat Random by 2% but Random had σ of 4%."

---

## Account-by-account design

### Account 1 — Baselines ($1,000,000 paper, 4 profiles × $250,000)

**What this account proves:** *whether the AI adds value over null hypotheses at all.* If the answer is no, nothing else in the experiment matters.

Four profiles share equal capital so their results are directly comparable. They run on the same universe (large-cap stocks) and the same maximum-position-percent rules so universe drift doesn't confound the result.

| Profile | Capital | Strategy | What it answers |
|---|---|---|---|
| Buy-Hold SPY | $250,000 | `strategy_type='buy_hold'` | Null floor — system must beat this or product is worthless |
| Random A | $250,000 | `strategy_type='random'`, profile_id=N (RNG seed A) | Random null, replica (a) — combined with Random B bounds noise |
| Random B | $250,000 | `strategy_type='random'`, profile_id=M (RNG seed B) | Random null, replica (b) — combined with Random A bounds noise |
| **Full System Standard** | $250,000 | `strategy_type='ai'`, all `enable_*` flags ON | **The anchor** — every Account 2 ablation compares to this profile |

**Win condition for Account 1:**
- `Sharpe(Full System) > Sharpe(SPY) + 0.5` *(meaningful index-beating margin, not just noise)*
- `Sharpe(Full System) > max(Sharpe(Random A), Sharpe(Random B)) + 0.3` *(real stock-picking skill)*
- Volatility within `1.5× Sharpe(SPY)` *(not just leveraging into the result)*

**Failure modes this account exposes:**
- If Full System ≈ Buy-Hold SPY: the system is closet-indexing — paying AI costs for no benefit.
- If Full System ≈ Random: stock-picking adds nothing — the alt-data + meta-model + specialists aren't doing useful work; investigate Account 2 ablations.
- If Random A and Random B diverge wildly: noise floor is too high to draw any conclusion from a 6-month window; need to run longer OR rethink universe.

---

### Account 2 — Ablations ($1,250,000 paper, 5 profiles × $250,000)

**What this account proves:** *which subsystems are actually generating the alpha that Account 1's Full System produces.* Each ablation removes ONE component (or a combination) while holding everything else identical to the Full System Standard from Account 1. Capital matches Account 1's anchor exactly so the comparison is clean.

| Profile | Capital | What's off | What it answers |
|---|---|---|---|
| No Alt-Data | $250,000 | `enable_alt_data=0` | Marginal value of the paid+scraped data feeds (insider, Congress, Form 4, 13F, sentiment, biotech catalysts) — if return ≈ Anchor, alt-data adds nothing and the cost/complexity should be dropped |
| No Meta-Model | $250,000 | `enable_meta_model=0` | Marginal value of the GBM + SGD calibration layer that maps raw AI confidence to a calibrated probability of win — if return ≈ Anchor, the calibration is doing nothing the AI doesn't already do |
| No Self-Tuning | $250,000 | `enable_self_tuning=0` | Marginal value of the 12-layer autonomous parameter learner — explicitly tests the post-2026-05-14-fix self-tuner. If return < Anchor, self-tuner is helping; if return > Anchor, self-tuner is hurting (worth knowing) |
| No Options | $250,000 | `enable_options=0` | Marginal value of the options pipeline (single-leg + multi-leg). After the 2026-05-13 episode that cost $200K on options, this is the most operationally important ablation — if return ≥ Anchor, options should be permanently disabled |
| **No Alt-Data + No Meta-Model** | $250,000 | both off | **Combined ablation** — tests whether the two components are complementary (combined loss > sum of individual losses → keep both) or redundant (combined loss ≈ max of individual losses → keep only the cheaper one) |

**Why the combined arm matters:** With only ~6 months of paper data, single-axis ablations may not have enough resolved trades to detect modest effects (especially alt-data, where the high-signal events are rare). The combined arm produces a bigger and easier-to-detect delta, AND it surfaces a question single ablations can't answer: *do the components reinforce each other or substitute for each other?* If alt-data and meta-model are redundant, we can drop the more expensive one. If they're complementary, we keep both even if each one's individual contribution looks marginal.

**Win condition for Account 2:**
- Every single ablation should underperform the Anchor by some amount. If an ablation **matches or beats** the Anchor, that component is a cost center.
- The combined ablation either equals the sum of the two singles (additive — both real, independent) or is less negative than the sum (redundant — keep one) or more negative than the sum (complementary — keep both).

**Failure modes this account exposes:**
- Any individual ablation ≥ Anchor → drop that component from prod (saves cost, reduces complexity).
- Combined ≈ singles average → components are redundant, pick the cheaper.
- Combined >> sum of singles → components are complementary, the system needs both.
- All ablations ≈ Anchor → none of the individual components matter; the AI is doing all the work just from price/volume, and we're paying for nothing.

---

### Account 3 — Product candidate + scale ($750,000 paper, 4 profiles)

**What this account proves:** *whether the system is ready for real $25K cash, whether the result is signal or luck, and whether lifting constraints would yield more alpha than the conservative default.*

This account contains three distinct experiments:

#### Experiment 3.A — The $25K real-money question (2 profiles)

| Profile | Capital | Notes |
|---|---|---|
| **$25K Candidate** | $25,000 | The configuration that, if it wins, the user deploys with real $25K cash |
| $25K Replica | $25,000 | Same configuration, different `profile_id` (different RNG) |

**The $25K Candidate configuration** (constrained best-of-all-strategies — what works at small capital):
- `max_total_positions = 5` (concentration over diversification at this size)
- `max_position_pct = 0.20` (up to 20% per position; allows conviction)
- `enable_short_selling = 0` (shorts tie up too much margin at $25K)
- Options: single-leg only (multi-leg spread margin makes them infeasible at $25K)
- `enable_alt_data = 1`, `enable_meta_model = 1`, `enable_self_tuning = 1`, `enable_options = 1`
- Universe: liquid mid-to-large caps (no illiquid microcaps where $25K is the whole float)

**What having a Replica proves:** if the Candidate produces a positive return, the question becomes "is this signal or did we get lucky?" The Replica runs the identical configuration with a different RNG seed. If both Candidate and Replica land within ±5% of each other AND both are positive, the result is signal. If they diverge by more than 5%, the strategy is too RNG-sensitive — investigate before deploying real money.

#### Experiment 3.B — The conservative-scaling question (1 profile)

| Profile | Capital | Notes |
|---|---|---|
| $250K Conservative Scale | $250,000 | Same constraints as the Candidate, 10× the capital |

**What this proves:** *does the strategy survive at 10× notional?* Slippage scales roughly with sqrt(position size). A strategy that works at $25K may degrade at $250K because the same fractional position size means trading larger dollar amounts, which means worse fills. If $250K returns are within 30% of the Candidate's returns (e.g., Candidate +12%, $250K +9%), the strategy scales linearly enough to consider larger real deployments. If $250K returns are <50% of the Candidate's, slippage is eating the alpha and real-money deployment should be capped at the largest size where returns hold.

#### Experiment 3.C — The "what if we let it cook" question (1 profile)

| Profile | Capital | Notes |
|---|---|---|
| **$450K Aggressive Free** | $450,000 | All small-capital constraints DROPPED: `max_total_positions = 15`, `max_position_pct = 0.10`, `enable_short_selling = 1`, multi-leg options allowed, longer hold periods, broader universe |

**What this proves:** *is the conservative default leaving alpha on the table, or is the conservative default actually the right risk/return tradeoff?*

The $25K Candidate is conservative because it has to be — it can't hold 15 positions when each position would be $1,600, can't afford multi-leg options where one spread eats half the account, can't take shorts that tie up margin. Those constraints exist because of capital, not because they're optimal. At $450K, none of those constraints bind. The Aggressive Free profile lets the AI run with all of its tools — every signal source ON, every position type allowed, more diversification, longer hold periods.

Two possible outcomes, both informative:
- **Aggressive beats Conservative Scale by margin > slippage cost of scale** → the conservative defaults ARE leaving alpha on the table. The $25K cash deployment is conservative for capital reasons, not strategy reasons — and as real-money capital grows, lifting constraints will unlock more return.
- **Aggressive performs same or worse than Conservative Scale** → the conservative defaults aren't a constraint, they're the actual optimal config. Concentration + simple stocks + no shorts + single-leg only is the strategy. Even with more capital we shouldn't loosen — the AI is already running at its best.

**Win condition for Account 3:**
- $25K Candidate AND $25K Replica both: positive return after costs, Sharpe > 1.0, max drawdown < 15%, σ between them < 5% → **deploy real $25K cash**.
- $250K Conservative Scale: degrades no more than 30% in Sharpe vs Candidate → the strategy scales; future real-money deployments can grow.
- $450K Aggressive Free: result determines whether to lift constraints as real-money capital grows.

**Failure modes this account exposes:**
- Candidate positive but Replica negative (or vice versa) → strategy is too RNG-dependent, don't deploy.
- Candidate and Replica both negative → strategy doesn't work at $25K; either redesign or shelve.
- Candidate wins but Conservative Scale loses → strategy works at $25K because the broader market favored small-position strategies during this window; scaling reveals it's noise.
- Aggressive Free crushes Candidate but Conservative Scale is flat → the $25K cash deployment is going to underperform what's possible with more capital; weigh against the "prove it with small money first" rationale.

---

## Total capital and profile count

| Account | Capital | Profiles |
|---|---|---|
| Account 1 — Baselines | $1,000,000 | 4 |
| Account 2 — Ablations | $1,250,000 | 5 |
| Account 3 — Product + scale | $750,000 | 4 |
| **Total** | **$3,000,000** | **13** |

---

## Implementation status (2026-05-17)

| Component | Status |
|---|---|
| `strategy_type='buy_hold'` dispatch + Buy-Hold SPY logic | ✅ `simple_strategies.run_buy_hold_spy` (commit 778e2f0) |
| `strategy_type='random'` dispatch + deterministic-pick logic | ✅ `simple_strategies.run_random_stock_of_day` (commit 778e2f0) |
| `enable_alt_data` flag + gate | ✅ commit 559d788 |
| `enable_meta_model` flag + gate | ✅ commit 559d788 |
| `enable_options` flag + gate | ✅ commit 559d788 |
| `enable_self_tuning` flag | ✅ pre-existing |
| `enable_short_selling` flag | ✅ pre-existing |
| Comparative-returns chart (overlays all profiles vs baselines on dashboard) | ✅ commit 37cdbf4 |
| Seven-tier integrity contract + 10-minute audit_runner | ✅ commits c2c6e47, 07dea6f, b6420de, 40c0f1c, 917c040 |
| Options P&L auto-cutoff (#171 — prevents the 2026-05-13 episode) | ✅ commit f14f5f2 |
| Orphaned-profile cleanup script | ✅ `clean_orphaned_profiles.py` (commit 778e2f0) |
| Morning health check | ✅ `morning_health_check.sh` (commit e196b4d) |

**Every arm of this design works in the current code. Remaining work is operational: clean orphans → user creates 3 new Alpaca accounts → batch-create the 13 profiles → run morning health check to confirm.**

---

## Success criteria summary (decision matrix)

After 6 months of paper trading on this design, the outcomes drive specific actions:

| Outcome | Action |
|---|---|
| Full System Sharpe > both Random replicas Sharpe AND > Buy-Hold + 0.5 | System validated — proceed to ablation interpretation |
| Full System ≈ Buy-Hold SPY | System is closet-indexing — investigate or shelve |
| Random A and Random B diverge by σ > 4% | Noise floor too high — extend window or simplify universe |
| $25K Candidate AND Replica both positive, Sharpe > 1.0, DD < 15%, σ between them < 5% | **Deploy real $25K cash** |
| $25K Candidate positive but Replica fails OR they diverge > 5% | Don't deploy — strategy too RNG-dependent |
| $250K Conservative Scale degrades > 30% vs Candidate | Strategy doesn't scale; cap future deployments at the largest size that holds |
| $450K Aggressive Free crushes Conservative Scale | Conservative defaults are leaving alpha on the table — as real capital grows, lift constraints |
| Any individual ablation ≥ Anchor | That component is a cost center — remove from prod |
| Combined ablation (NoAlt+NoMeta) loss >> sum of singles | Components are complementary — keep both even if individual contributions look marginal |
| Combined ablation loss ≈ singles average | Components are redundant — keep the cheaper one |

---

## Reset / kept on launch

**Wiped** (via `reset_for_clean_experiment.py --apply`):
- Per-profile journal tables: `trades`, `ai_predictions`, `virtual_profile_state`, `ai_cost_ledger`, `activity_log`
- Optionally (`--wipe-ai-memory`): `specialist_outcomes`, `tuning_history`, `learned_patterns`, `meta_model_state`, `strategy_validations`, `ai_shadow_calls`
- All open Alpaca broker positions (via `--close-broker`) — fresh experiment must not inherit legacy positions

**Removed entirely** (via `clean_orphaned_profiles.py --apply`):
- The 10 stale per-profile DB files + `trading_profiles` rows from the old Alpaca accounts the user deleted earlier on 2026-05-17

**Kept:**
- `quantopsai.db` itself (alpaca_accounts, users, audit_alerts table)
- Altdata DBs (insider, congresstrades, edgar13f, edgar_form4, biotechevents, stocktwits) — world data
- Code, tests, deployment infra

---

## Stop / retune / restart conditions

The 12 batches of code shipped 2026-05-17 are *tested* (3395 tests) and *audited* (seven-tier integrity contract running every 10 min) but most of the new code has never run against real broker activity. The first 2 weeks are a system shakeout, not a measurement window. The actual ablation-comparison clock starts on day 15 if days 1-14 ran clean.

### Things most likely to misbehave (calibrated uncertainty)

1. **`activities_capture`** has never received a real dividend or option assignment from Alpaca. Field names verified against the canonical Alpaca docs 2026-05-17 (`net_amount` for DIV dollar amount, `symbol` carrying OCC for option events) and pinned by `TestAlpacaFieldContract` regression tests so any drift from the documented shape won't be silent. Remaining uncertainty: the Alpaca docs don't explicitly list a `price` field for NonTradeActivity — the code defaults to 0 when absent, which IS the correct close-out price for OPEXP/OPASN/OPXRC (the actual cash and share movements arrive as separate FILL activities, captured by the existing order-id reconciler). The cash-parity audit fires within 10 min if any drift slips through.
2. **Options P&L auto-cutoff (`_optimize_options_pnl_cutoff`)** has never had real options trades to sum. Edge cases in `occ_symbol` tagging on FIFO-matched rows could cause it to under- or over-count, firing when it shouldn't or staying silent when it should.
3. **`audit_runner` email path** has never sent a real production email. If SMTP is misconfigured on the droplet, `alert_sent` stays at 0 (which IS the correct retry behavior), but the operator may not see drift until they check `/issues` or the audit_alerts table directly.
4. **`simple_strategies` dispatch in `multi_scheduler._task_scan_and_trade`** has never run a real profile. If `run_buy_hold_spy` errors on the first SPY fetch, the profile sits idle and the activity-log entry looks normal.

### Tripwires

| Severity | Condition | Action |
|---|---|---|
| **HARD STOP — restart everything** | `audit_alerts` table accumulates >5 unresolved drift items in any 24h window across any audit type | Pause via `kill_switch.set_killed(True)`. Investigate root cause. Fix code. Re-run `reset_for_clean_experiment.py --apply`. Drift means measurements are unreliable; any return data collected during the drift is suspect. |
| **HARD STOP** | Any profile loses >5% of `initial_capital` in 24h OR >12% over 7 days | Kill switch on that profile (`enabled=0`). Determine cause: bug in new code? broker rejection cascade? legitimate market move? Restart that profile only OR call the experiment. |
| **HARD STOP** | Equity-identity drift fires on ≥2 profiles simultaneously | Almost certainly a bug in journal cash math or activities_capture. Kill switch, fix code, re-run reset script for affected profiles. |
| **HARD STOP** | Reconciler heartbeat stale on ≥3 profiles for >2 hours | Scheduler or host problem. Audits are reading frozen state, can't trust their "clean" status. Investigate scheduler immediately. |
| **HARD STOP** | Any single drift signature stays unresolved >48h | The drift isn't auto-healing; needs manual diagnosis. Pause new entries on affected profiles until cleared. |
| **SOFT RETUNE** | A single ablation profile shows zero trades for 7+ days while the Anchor is trading normally | The `enable_X=0` gate is too aggressive OR a code path got broken. Diff behavior, fix, restart THAT profile only (no full reset). |
| **SOFT RETUNE** | Options auto-cutoff fires on >2 profiles in the same week | Either options ARE that bad (signal — keep them off, cutoff did its job) OR the cutoff calculation is wrong. Hand-audit the trades. If trade data looks right, accept the signal. If trade data looks wrong, fix the calculation and restart options for those profiles. |
| **SOFT RETUNE** | Random A and Random B diverge by σ > 5% after 30+ days | Variance bound is broader than expected — extend window OR add a third Random replica before drawing conclusions about Full System vs Random. |
| **SOFT RETUNE** | A single profile's daily-snapshot capture missing for ≥2 consecutive days | Daily snapshot task failing on that DB specifically. Investigate (likely schema or disk issue); don't reset experiment data. |
| **CONTINUE — experiment is working** | Individual ablation underperforms Anchor by 1-4% | That IS the answer the experiment was designed to produce. Don't intervene. |
| **CONTINUE** | Drift surfaces and resolves within hours via the audit_runner | Defense working as designed. |
| **CONTINUE** | Profitability fluctuates within ±3% week-over-week | Normal noise; let it run. |

### Operating procedure during the experiment

- **Every morning**: run `./morning_health_check.sh`. Any FAIL = investigate before market open.
- **Weekly**: open `/dashboard`, eyeball the comparative-returns chart, compare each profile's curve to the Anchor and the baselines. Anything visually anomalous (a profile flatlining, a wild outlier, the Random replicas diverging) = drill in.
- **On any first-detection email**: the email from `audit_runner` is the FIRST chance to catch a problem. Don't snooze it — drift items get one alert each (no spam), so the next one is the next NEW problem.
- **Quarterly**: re-read this section, especially the "Things most likely to misbehave" list. Tick off any item the experiment has now exercised without incident.

### When to call the experiment vs restart

Call the experiment (stop, accept the data so far as inconclusive, redesign) if:
- Hard-stop tripwire fires more than twice during the same 30-day window — pattern, not flake.
- The seven-tier audit produces drift you can't explain in code review — the integrity guarantees aren't holding for some reason; fix the foundations before continuing.

Restart specific profiles only (keep the experiment running) if:
- A soft-retune tripwire fires and is diagnosed to a single profile's configuration or a single component, not a systemic issue.
- The fix is small + localized (e.g., a stuck ablation gate, a specific symbol causing problems).

---

## Launch sequence

1. `./morning_health_check.sh` — sanity-check current prod state.
2. `python3 clean_orphaned_profiles.py` — dry-run; review the list of profiles to be removed.
3. `python3 clean_orphaned_profiles.py --apply` — execute removal.
4. **User creates 3 new Alpaca paper accounts** in the Alpaca dashboard, funds them:
   - Account 1: $1,000,000
   - Account 2: $1,250,000
   - Account 3: $750,000
5. **User adds the 3 accounts to QuantOps** via the settings page (paste the API keys).
6. **Batch-create the 13 profiles** via a script (TBD — config dict per the tables above, calls `models.create_trading_profile`).
7. `./morning_health_check.sh` — confirm all 13 profiles discovered, audit_alerts empty, reconciler heartbeat green, comparative-returns API returns 13 series at 0% return.
8. **Let it run.** First useful comparative data emerges once each profile has ~5 daily snapshots + a few completed trade cycles (≈ 2 weeks).

---

## Open implementation work

- Batch-insert script that takes a config dict (13-profile manifest) and calls `models.create_trading_profile` for each — saves 13 manual form fills.
- (Done) Everything else — every arm of this design uses code already shipped today.
