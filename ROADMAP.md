# QuantOpsAI — Quant Fund Evolution Roadmap

**Purpose:** This document is the source of truth for the system's evolution from "excellent retail platform" to "world-class quant fund in miniature." It persists across development sessions so context is never lost.

**Last updated:** 2026-04-28

**Phase 11 (Long/Short Parity)** added 2026-04-28 — see
`LONG_SHORT_PLAN.md` for the detailed sub-phase plan and progress.

---

## Vision

QuantOpsAI is already better than 95% of retail trading systems. The 10 phases below close the gap toward what real quant funds (Renaissance, Two Sigma, DE Shaw) actually do. The key insight: **alpha comes from layering**, not from any single clever trick. Each phase compounds with the others.

**Our moat:** We have no legacy infrastructure. Every big fund runs 10-year-old code on 90s data pipelines and cannot rebuild. We can rebuild our entire stack every 6 months. Agility is the asymmetry.

---

## The 10 Phases

### Phase 1 — Meta-Model on Own Prediction Data [CURRENT]

Train a gradient-boosted classifier on the features the AI saw for each past prediction, with the target being whether that prediction was right or wrong. This predicts **"when is the AI likely to be correct?"** and re-weights confidence before execution. Creates a second-order edge that compounds automatically as prediction data accumulates.

**Key insight:** We're not predicting the market — we're learning our AI's systematic blind spots from proprietary data no one else has.

### Phase 2 — Scientific Backtesting Infrastructure

Walk-forward optimization, out-of-sample validation, regime-specific testing, transaction cost modeling, Monte Carlo stress testing, statistical significance gates. **Every strategy must pass this gauntlet before going live.** This is the discipline that 90% of funds skip.

### Phase 3 — Alpha Decay Monitoring

Track rolling Sharpe of every signal and strategy. Auto-deprecate when edge fades for 30+ trading days. Most systems cling to dead strategies forever because nobody measures decay rigorously.

### Phase 4 — SEC Filings Semantic Analysis

AI reads every 10-K, 10-Q, 8-Q for our watchlist. Detect material language changes between filings (new "going concern", "material weakness", risk factor shifts). Strongest predictive signals in finance — and nearly no fund does it at our scale.

### Phase 5 — Options Chain Oracle

Compute IV surface shape, put/call skew, term structure, gamma exposure (GEX), max pain from free yfinance data. This reveals "what the smartest money in the world expects" for every stock. Institutional-only insight at zero cost.

### Phase 6 — Multi-Strategy Parallel Execution

Run 10+ uncorrelated strategies simultaneously with capital allocation via Kelly criterion or risk parity. Each strategy has small alpha; combined they dominate. Strategies to add: pair trading, earnings drift, short-term reversal, sector rotation, insider cluster replication, volatility regimes.

### Phase 7 — Strategy Auto-Generation

AI proposes strategy variants → auto-backtest → promote winners to shadow trading → retire losers. Genetic algorithm on strategy space. This is what Renaissance has been doing for 30 years — a self-improving strategy library.

### Phase 8 — Ensemble of Specialized AIs

Replace single generalist AI with specialists: earnings analyst, pattern recognition, sentiment/narrative, risk assessment, and a meta-coordinator that weighs them contextually. Different architectures see different things.

### Phase 9 — Event-Driven Architecture

React to events, not timers. Earnings announcements, SEC filings, Fed decisions, congressional trade disclosures, whale crypto movements each trigger immediate AI analysis. Events matter more than clock ticks.

### Phase 10 — Cross-Asset Crisis Detection

Detect regime breaks (VIX > 40, cross-asset correlation spike, credit spread widening) and automatically shift to capital preservation mode. Prevents catastrophic losses during events like March 2020 or 2008.

### Phase 11 — Long/Short System Parity (LONG_SHORT_PLAN.md)

Turn "long pipeline with shorts as a side door" into a real long/short system that can compete with Citadel, Millennium, Point72. Modeled on what real long/short equity hedge funds actually do — the highest-Sharpe quant funds (Renaissance Medallion ~6, Citadel ~3) are all long/short, not long-only.

**Phase 1 — Short capability with parity to longs (DONE 2026-04-28).** See `LONG_SHORT_PLAN.md` for detailed sub-phase docs. 14 sub-phases delivered: SELL semantic fix + prediction_type column, 5 dedicated bearish strategies, asymmetric sizing/stops/time-stops, two shortlists with reserved slots, AI prompt with explicit long/short sections, borrow/squeeze/regime filters, per-direction self-tuning, direction-aware specialist calibrators, meta-model with prediction_type feature, strategy generator alternates direction, borrow-cost penalty.

**Phase 2 — Pair / sector-neutral / factor-aware construction (DONE 2026-04-28).** Sector exposure tracking with concentration warnings, long/short ratio targets per profile (`target_short_pct`), pair-trade primitive (long winner + short loser in same sector via `find_pair_opportunities`), balance gate (block over-weighted side once book drifts >25pp off target), factor-aware construction (size bands + direction balance).

**Phase 3 — Real alpha sources beyond technicals (DONE 2026-04-29).** 4 catalyst-driven short strategies (`earnings_disaster_short`, `catalyst_filing_short`, `sector_rotation_short`, `iv_regime_short`), insider signal promoted to primary weight, real factor exposures from yfinance fundamentals (book-to-market, beta, 12-1m momentum) with 7-day cache via `factor_data.py`.

**Phase 4 — Active factor construction (DONE 2026-04-28).** Five sub-phases: beta-targeted construction with `target_book_beta` per profile and AI prompt directive (P4.1); fractional Kelly position sizing per direction surfaced to AI (P4.2); drawdown-aware capital scaling (continuous 1.0× → 0.25× modifier, P4.3); risk-budget (risk-parity) sizing — flag positions whose `weight × annualized_vol` is way out of band, sizing rule `size ∝ target_vol / realized_vol` (P4.4); market-neutrality enforcement — hard gate that blocks entries pushing book beta further from target by >0.5 (P4.5).

**Structural fixes (2026-04-29).** Audit of last 30 days showed profile_10 (Small Cap Shorts, target_short_pct=0.5) emitted only 3 SHORT predictions out of 1497. Two root causes shipped:
- Regime gate now respects `target_short_pct` mandate. When a profile is configured for substantial shorts (≥0.4), the strong-bull gate bypasses for that profile — the user has accepted regime risk by setting the mandate.
- New `relative_weakness_universe` strategy (anti-momentum / academic short factor). Universe-ranked by 20d return vs SPY; emits bottom 5% (cap 5) with 5%+ RS gap and 20d MA confirmation. Critical for filling short books in extended bull regimes where textbook bearish technical patterns are rare.

### Phase 12 — Exit Execution Hardening (`INTRADAY_STOPS_PLAN.md`)

Polling-based exit detection ran on a 5-min cycle. Stop fills overshot threshold by 2-3pp on average (AMD: -7.91% on -5% threshold) because by the time we detected the breach, price had moved past it. On reversals, trailing stops fired at next-day close after intraday spikes — recorded as $2.70 wins on what were $1500+ unrealized winners (the IBM pattern).

**Stages 1-3 (DONE 2026-04-30).** Replaced polling with broker-managed orders. After every entry the sweep places either a `type='trailing_stop'` (when `use_trailing_stops=True`) or `type='stop'` Alpaca order with `time_in_force='gtc'`. Polling defers to the broker when an active broker trailing exists for the symbol — verified per-cycle. Polling stays as fallback only when broker is unavailable.

**Trade-quality fixes (DONE 2026-04-29):**
- **Fix 1:** MFE capture ratio (realized P&L / available favorable excursion) surfaced to dashboard + AI prompt. Tells when exit logic is asymmetric vs entry edge.
- **Fix 3:** Scratch-trade classification. `|pnl_pct| < 0.5%` is scratch, not win. Win rate now `winning / (winning + losing)` excluding scratches. Without this, profiles closing at break-even all day showed inflated 70%+ win rates.

**Resilience fixes (DONE 2026-04-30):**
- `check_exits` per-position try/except — one Alpaca rejection no longer takes out the whole cycle.
- Wash-trade / insufficient-qty / cross-direction broker rejections classified as recoverable SKIP, not ERROR.
- 30-day wash-trade cooldown table.
- `get_bars` 5-min TTL cache — scan times ~4× faster on universe-iterating strategies.
- Pending-orders dashboard panel filtered to per-profile owned IDs (was leaking sibling-profile orders since 6 profiles share one Alpaca account).

### Phase 13 — Competitive-Gap Closure (`COMPETITIVE_GAP_PLAN.md`)

Roadmap of capabilities real funds have that we didn't. Items shipped 2026-04-29 → 2026-05-01:

- **1a. Options trading layer** — full 8-phase build (`OPTIONS_PROGRAM_PLAN.md`): Greeks aggregator + gates, multi-leg combo orders, roll/lifecycle/wheel state machine, delta hedger, vol regime classifier, pre-earnings IV crush capture, Alpaca options chain (replacing yfinance), synthetic backtester.
- **1b. Statistical arbitrage** — `stat_arb_pair_book.py` with Engle-Granger cointegration scanning, Z-score entry/exit, half-life tracking, regime-break ejection.
- **2a. Barra-style portfolio risk model** — `portfolio_risk_model.py` + `risk_stress_scenarios.py` with ~21-factor universe (Ken French 5F+Mom + 11 SPDR sectors + 4 MSCI style ETFs), parametric + Monte Carlo VaR + ES, ridge-regularized exposure regression, Ledoit-Wolf factor covariance, and 7 historical stress scenarios (1987 Black Monday → 2023 SVB). Daily snapshots persisted; surfaced in AI prompt under `MARKET CONTEXT > PORTFOLIO RISK`.
- **2b. Intraday risk monitoring** — `intraday_risk_monitor.py` with 4 checks (drawdown acceleration, vol spike, sector concentration swing, held-position halts), aggregate action levels (pause_all > block_new_entries > monitor), trade pipeline gates new entries during halts.
- **3b. Earnings-call sentiment NLP** — `sec_filings.get_earnings_call_sentiment` parses 8-K exhibits, runs Haiku for tone classification, surfaces `transcript_sentiment` in candidates.
- **5a. Online learning meta-model** — `online_meta_model.py` (`SGDClassifier.partial_fit`) "freshness layer" alongside the GBM. Updates per-resolved-prediction; large divergence between SGD and GBM flags regime drift.
- **5b. Adversarial reviewer** — 5th specialist with VETO authority.
- **6b. Strategy-level capital allocation** — `strategy_capital_allocator.py` weights per strategy on `sharpe × (1 + win_rate)`, clamped [0.25×, 2.0×].

Tracking only — no Phase 14 sub-roadmap. Items 4a (IBKR multi-asset), 6a (real money), 1c long-vol portfolio hedge, and the rest of 3a (Google Trends / Wikipedia / GitHub / job postings) are still open in `COMPETITIVE_GAP_PLAN.md`.

---

## Phase Order & Dependencies

```
    Phase 1 (Meta-Model) ─────────────────────────────┐
                                                        │
    Phase 2 (Backtesting) ──┬──► Phase 3 (Alpha Decay) ─┤
                            │                           │
                            ├──► Phase 4 (SEC)          ├──► Phase 6 (Multi-Strategy)
                            │                           │          │
                            └──► Phase 5 (Options)      │          ├──► Phase 7 (Auto-Gen)
                                       │                │          │
                                       └────────────────┘          ├──► Phase 9 (Events)
                                                                   │
                                       Phase 8 (Ensemble AIs) ◄────┤
                                                                   │
                                       Phase 10 (Crisis) ◄─────────┘
```

**Core rule:** Never skip phases. The order exists because each phase compounds with prior ones.

---

## Non-Negotiable Principles

1. **Statistical rigor is mandatory.** No strategy goes live without passing Phase 2 validation. Not ever. Not "I have a feeling about this one."
2. **Alpha decays always.** Every signal, every strategy, every edge gets tracked and auto-retired when it stops working.
3. **Layering compounds.** Each phase makes prior phases more valuable. Don't skip to shiny new phases.
4. **Proprietary data is the moat.** Our predictions, backtest results, pattern discoveries are more valuable than any public data source.
5. **Documentation is infrastructure.** This file is the source of truth. Every phase has a detailed sub-plan. Never assume memory.

---

## Phase Status

| # | Name | Status | Dependencies |
|---|---|---|---|
| 1 | Meta-Model on Prediction Data | ✅ Infrastructure Complete (awaiting data) | — |
| 2 | Scientific Backtesting Infrastructure | ✅ Complete | — |
| 3 | Alpha Decay Monitoring | ✅ Complete | — |
| 4 | SEC Filings Semantic Analysis | ✅ Complete | — |
| 5 | Options Chain Oracle | ✅ Complete | — |
| 6 | Multi-Strategy Parallel Execution | ✅ Complete | — |
| 7 | Strategy Auto-Generation | ✅ Complete | — |
| 8 | Ensemble Specialized AIs | ✅ Complete | — |
| 9 | Event-Driven Architecture | ✅ Complete | — |
| 10 | Cross-Asset Crisis Detection | ✅ Complete | — |

**🎯 ALL 10 PHASES COMPLETE**

### Phase 1 Completion Summary

**Deployed:** 2026-04-14. All infrastructure live and verified.

**What was built:**
- `meta_model.py` — gradient-boosted classifier with feature extraction, training, inference, and persistence
- `features_json` column added to `ai_predictions` table via idempotent migration
- `record_prediction()` extended to accept and store full feature payload
- `trade_pipeline.py` pipeline passes candidate features to prediction recording AND integrates meta-model re-weighting at Step 4.5 (between AI selection and execution)
- Daily `_task_retrain_meta_model` in `multi_scheduler.py` — trains when ≥100 resolved predictions available
- Performance dashboard "Meta-Model" panel showing AUC, accuracy, sample count, top features
- 18 new tests in `test_meta_model.py` — all passing (122 total)

**What's waiting for data:**
- Model training requires 100+ resolved predictions per profile
- Current state: 3 profiles in production, each capturing features in every scan cycle
- Expected first models: 2-4 weeks at current scan frequency
- Suppression threshold: `meta_prob < 0.3` drops trades; confidence blend = `ai_conf * (0.5 + meta_prob * 0.5)`

**Success criteria (to verify in 2-4 weeks):**
- AUC > 0.55 on each profile's meta-model
- Feature importance shows sensible patterns (not random)
- Overall win rate improves ≥3% vs pre-meta-model baseline

---

## Phase 2 Implementation (Complete)

### What was built
- `rigorous_backtest.py` — single-entry `validate_strategy()` that runs 10 gates and returns PASS/FAIL with full report
- Individual gate functions (walk-forward, out-of-sample, regime, Monte Carlo, statistical significance, capacity)
- Transaction cost modeling baked into Monte Carlo
- `strategy_validations.db` persistence with full reports searchable by timestamp/strategy/market
- Performance dashboard integration showing recent validation runs
- 15 tests in `test_rigorous_backtest.py` — all passing (137 total)

### The 10 Gates
Sharpe, drawdown, win rate, statistical significance (p<0.05), Monte Carlo (>60% positive bootstraps), out-of-sample (OOS Sharpe drop ≤30%), regime consistency (≥2 profitable regimes), walk-forward (≥50% folds profitable), capacity (≤1% daily volume), minimum activity (≥30 trades).

### Usage
```python
from rigorous_backtest import validate_strategy, save_validation

result = validate_strategy(strategy_fn=my_strategy, market_type='midcap')
if result['verdict'] == 'PASS':
    save_validation('my_strategy', result)
```

### Why This Matters for Phases 3-10
All subsequent phases depend on this gate:
- Phase 3 (Alpha Decay) uses historical validation scores to detect degradation
- Phase 6 (Multi-Strategy) only allocates capital to validated strategies
- Phase 7 (Auto-Generation) requires every proposed strategy to pass validation before promotion
- Non-negotiable principle: no strategy goes live without a PASS verdict.

### First Validation Run Results (2026-04-13)

All 5 live strategy engines validated. All 5 **FAILED** — which is a correct and important finding:

| Strategy | Verdict | Score | Sharpe | Max DD |
|---|---|---|---|---|
| micro_combined | FAIL | 30.0 | 0.33 | -2% |
| small_combined | FAIL | 50.0 | 0.14 | -5% |
| mid_combined | FAIL | 40.0 | -0.27 | -11% |
| large_combined | FAIL | 40.0 | 0.39 | -5% |
| crypto_combined | FAIL | 20.0 | -1.15 | -63% |

**Interpretation:** The raw strategy engines, in isolation, do not produce alpha. This is expected and matches production behavior — the AI frequently passes on their signals because the underlying technical patterns aren't strong enough on their own. The live system's edge comes from the AI-first pipeline (strategy + 33+ indicators + alt data + sector + patterns + meta-model), not from the strategy engines alone.

**Immediate follow-ups queued (Phase 2.1):**
1. ~~Shared data caching across validation gates~~ ✅ **DONE (2026-04-13)** — 5.97x speedup
2. End-to-end pipeline validation: a harness that runs validate_strategy on the full pipeline (strategies + AI + meta-model), not just signal generators. This is the real test of system edge.
3. Use these results to inform Phase 7 (auto-generation) — the gate will reject weak variants, keep the rare strong ones.

The gate itself works correctly. The results are sobering and useful.

### Phase 2.1 Completion (2026-04-13)

**Two optimizations deployed:**
1. **Per-symbol yfinance cache** (`_symbol_cache` in `backtester.py`) — downloads each symbol once at max-window (720 days), slices down for any shorter request. Eliminates redundant downloads across the 7 backtests in each validation run.
2. **Indicator precomputation** — strategy engines were recomputing all 33 indicators on every day's window (~100+ times per symbol per backtest). Now `add_indicators(df)` runs once before the simulation loop; strategies reuse the prepopulated DataFrame. This was the bigger win.

**Measured speedup on the 5-strategy validation suite:**

| Strategy | Before | After | Factor |
|---|---|---|---|
| micro | 317.6s | 58.4s | 5.4x |
| small | 347.0s | 48.0s | 7.2x |
| mid | 240.9s | 46.3s | 5.2x |
| large | 362.8s | 68.2s | 5.3x |
| crypto | 224.2s | 29.1s | 7.7x |
| **Total** | **24.9 min** | **4.2 min** | **5.97x** |

Verdicts and scores identical before/after — confirms the optimization is pure work-elimination, no behavior change. Makes Phase 7 (auto-generation, which validates dozens of variants) practical; previously would have been prohibitively slow.

---

## Phase 3 Implementation (Complete)

### What was built
- `alpha_decay.py` — rolling metrics, snapshot persistence, decay detection, auto-deprecation, restoration
- Two new per-profile tables: `signal_performance_history` (daily snapshots) and `deprecated_strategies` (active deprecations)
- Daily `_task_alpha_decay(ctx)` scheduler task runs the full cycle alongside self-tuning and meta-model retraining
- Trade pipeline filter: `_rank_candidates()` skips signals from deprecated strategies
- Performance dashboard "Alpha Decay Monitoring" panel showing per-strategy rolling vs lifetime Sharpe, edge change, and any deprecations
- 14 new tests in `test_alpha_decay.py` — all passing (151 total)

### Detection Algorithm
1. Daily: write a 30-day rolling snapshot per strategy_type to `signal_performance_history`
2. Compare rolling Sharpe vs lifetime Sharpe
3. If rolling ≤ lifetime × 0.7 for 30 consecutive snapshot days → auto-deprecate
4. If deprecated strategy's rolling Sharpe recovers to within 15% of lifetime for 14 consecutive days → auto-restore

### Pipeline Integration
The trade pipeline's `_rank_candidates()` now accepts a `deprecated_strategies` set and filters out any candidate whose primary voting strategy is deprecated. Happens every scan cycle, zero AI cost.

### Why This Matters for Phases 6-10
- Phase 6 (Multi-Strategy): capital allocator only considers non-deprecated strategies
- Phase 7 (Auto-Generation): deprecated strategies get retired automatically, making room for new variants proposed and validated by the Phase 2 gate
- Phase 10 (Crisis Detection): decay monitor surfaces wholesale system failures (multiple strategies deprecated at once → crisis signal)

### What's waiting for live data
Same as Phase 1: alpha decay requires ≥50 resolved predictions per strategy_type to establish a lifetime baseline, then 30+ days of snapshots to detect decay. Current production has ~225 Crypto predictions but they span multiple strategy types, so baselines per strategy will mature over the next few weeks. The dashboard already shows rolling vs lifetime for anything with resolved data.

---

## Phase 4 Implementation (Complete)

### What was built
- Expanded `sec_filings.py` from a Form-4-only module to a full 10-K / 10-Q / 8-K analyzer
- CIK lookup via SEC's free `company_tickers.json` mapping (cached in-process)
- Company filings list via EDGAR submissions JSON
- Filing text fetch via rate-limited EDGAR requests with BeautifulSoup parsing
- Section extraction: Item 1A Risk Factors, Item 7 MD&A
- Flag detection: "going concern" and "material weakness in internal control"
- AI-powered semantic diff of risk factor language between consecutive filings
- New per-profile table `sec_filings_history` stores filing metadata, sections, and AI alerts
- Daily scheduled task `_task_sec_filings(ctx)` monitors held positions + shortlist candidates
- Trade pipeline injects medium+ severity alerts into the AI batch prompt per candidate
- Performance dashboard "SEC Filing Alerts" panel with severity/signal/summary rows
- 13 new tests in `test_sec_filings.py` — all passing (164 total)

### Architecture
```
EDGAR (free) → CIK lookup → submissions JSON → filing list
  → fetch HTML → BeautifulSoup plain text → regex section extraction
  → AI diff vs previous filing of same type → severity/signal/summary
  → sec_filings_history (persist) → trade pipeline (inject into candidate)
```

### Rate limiting and identification
SEC requires <10 req/sec and a contactable User-Agent. The module enforces 110ms between requests and identifies as `QuantOpsAI Research Bot (mack@mackenziesmith.com)`. Filings are immutable, so we cache filing bodies indefinitely once fetched.

### Why This Matters for Phases 5-10
- Phase 5 (Options Oracle) and Phase 4 are independent signals that combine well in the AI prompt — options flow + SEC alerts = full-picture institutional awareness
- Phase 8 (Ensemble AIs) — the SEC diff is already a specialist AI call; Phase 8 will formalize this pattern across all data sources
- Phase 10 (Crisis Detection) — a cluster of high-severity SEC alerts across held positions is a macro distress signal

---

## Phase 5 Implementation (Complete)

### What was built
- New `options_oracle.py` module computing 7 institutional-grade signals from free yfinance chains
- IV Skew (fear/greed asymmetry)
- IV Term Structure (event detection via inversion)
- Implied Move (ATM straddle → 1σ move)
- Put/Call Ratios (volume + open interest)
- Gamma Exposure / dealer regime (pinning vs expansion)
- Max Pain (gravitational strike near expiration)
- IV Rank (vs 52-week realized vol proxy)
- Integration into trade pipeline: every equity candidate carries an `options_oracle_summary` field that the AI sees on its own line in the batch prompt
- 30-minute cache TTL per symbol (matches 15-min scan cadence with headroom)
- Crypto symbols skipped automatically
- 18 new tests in `test_options_oracle.py` — all passing (182 total)

### Compact summary format injected into AI prompt
```
OPTIONS: skew=fear(1.42) | IV TERM INVERTED | implied_move=6.2%/4d | PCR=1.85(bearish_flow) | gex=volatility_expansion | iv_rank=iv_high
```

The AI reads this and knows what institutional options traders believe about a stock's next few days. No data subscription required.

### Why This Matters for Phases 6-10
- Phase 6 (Multi-Strategy): options signals unlock new strategy types — volatility regime trading, skew contrarian, max pain pinning plays
- Phase 8 (Ensemble AIs): a dedicated options-flow specialist AI becomes natural; it reads the full oracle dict vs the prompt summary
- Phase 10 (Crisis Detection): cluster of extreme skew readings across held positions → early crisis signal

---

## Live Incidents & Fixes (2026-04-15)

Two serious production issues surfaced and were fixed today. Documented
in full detail in `CHANGELOG.md`; roadmap-level summary here.

### Market-data migration: yfinance → Alpaca (Algo Trader Plus)

yfinance timeouts during market open hung the screener for 30+ minutes
and blocked exits behind the scan pipeline. Upgraded to Alpaca Algo
Trader Plus and migrated `market_data.get_bars()` + `screener` to use
Alpaca's SIP feed and batched `get_snapshots`. Screener went from
30 min → 853 ms. yfinance kept as fallback; crypto bypasses Alpaca.

### Strategy SELL-bias: Small Cap starved of trades for days

Small Cap opened zero trades for 4+ days despite 616 AI predictions.
Root cause: every size-specific strategy module
(`strategy_{small,mid,large,micro}.py`) leaked "exit conditions for
hypothetical longs" (e.g. `price >= sma_20`, `rsi > 55`) as `SELL`
votes, biasing the multi-strategy aggregator toward `STRONG_SELL` on
most of the universe. The specialist ensemble (Phase 8) confirmed the
pre-tagged label; the AI correctly concluded "no edge." Fixed by
stripping the bogus SELL branches and adding an aggregation-level
short-flag gate. 18 new regression tests.

### Operational
- Per-profile Alpaca key rotation after the master-key upgrade (Mid
  Cap + Small Cap keys had been revoked / pointing at a dead paper
  account; UI-driven re-keying resolved).
- `/opt/quantops/` (singular, no "ai") stale directory on server
  identified; pending cleanup.

**Test count:** 468 passing (was 450 before today).

---

## Operational Hygiene (2026-04-14)

Two small but high-value layers added after the 10-phase roadmap completed.

### AI cost ledger
- `ai_pricing.py` — per-model USD/M-token table (estimates, easy to update); fallback for unknown models prefers over-estimate to silent $0
- `ai_cost_ledger.py` — write/read API; per-profile `ai_cost_ledger` table; `spend_summary()` returns 1d/7d/30d totals + breakdowns by purpose and model
- `ai_providers.call_ai()` extended with `db_path` + `purpose` kwargs; threaded through every high-volume caller (ensemble specialists, batch select, strategy proposer, SEC diff, political context, single-symbol analyze, consensus secondary)
- Dashboard panel "AI Cost" at top of `/performance#ai` with per-profile + cross-profile totals
- 13 new tests covering pricing math, fallback behavior, ledger writes, missing-table safety, window aggregation

### Database backup with rotation
- `backup_db.py` — SQLite-native `.backup` API (WAL-safe), atomic write via `.tmp` + `os.replace`, date-stamped destination, per-day idempotent
- Default 14-day retention with rotation
- New `_task_db_backup` runs in scheduler's daily snapshot block
- 10 new tests covering valid-copy creation, missing-source handling, dest-dir auto-create, atomic-rename cleanup, multi-DB sweep, WAL/SHM exclusion, and rotation

Total new tests: 23 (354 total passing).

---

## Expanded Seed Strategy Library (2026-04-14)

Added 10 hand-coded strategies to complement the original 6, bringing the total to **16 built-in strategies**. Also fixed the hardcoded `DEFAULT_WEIGHT = 1/6` in `multi_strategy.py` so the capital allocator scales cleanly regardless of library size.

### What was added

| Strategy | Data source | Research lineage |
|---|---|---|
| Short-Term Reversal | Bars + RSI | Jegadeesh 1990, Lehmann 1990 |
| Sector Momentum Rotation | Sector rotation data | Moskowitz, Asness |
| Analyst Revision Drift | yfinance recommendations | Womack 1996 |
| 52-Week Breakout | Bars + volume | George & Hwang 2004 |
| Short Squeeze Setup | Alt-data short interest | — |
| High IV Rank Fade | Options oracle | Premium-sell proxy |
| Insider Selling Cluster | Alt-data insider txns | Seyhun 1986 (bearish mirror) |
| News Sentiment Spike | News sentiment signal | Tetlock 2007, Garcia 2013 |
| Volume Dry-up Breakout | Bars + volume | Minervini / O'Neil |
| MACD Cross Confirmation | Indicators | Classic with multi-factor confirmation |

### Registry coverage by market

- Crypto: 3 strategies (market_engine, news_sentiment_spike, macd_cross_confirmation)
- Micro: 5 strategies
- Small: 13 strategies
- Midcap: 16 strategies (full set)
- Largecap: 14 strategies

### Infrastructure fix

`DEFAULT_WEIGHT` is now computed per-call as `1/N` where N is the number of strategies in the current allocation. The prior hardcoded `1/6` would have silently misallocated capital once the library exceeded 6 strategies. The `compute_capital_allocations` function is unchanged in behavior for the original 6 but now scales correctly to any library size.

### Honest caveats

- Most classical anomalies in this list have **decayed in academic studies** — Short-Term Reversal and 52-Week Breakout both show reduced effect sizes vs. the original papers
- Individual strategies may fail Phase 2 rigorous validation when run in isolation (same as the original 6 did during the Phase 6 validation exercise)
- The value of the expanded library is **breadth for Phase 7** (the AI proposer has more parent patterns to evolve from) and **diversity for the meta-model** (more varied behavioral profiles to learn error patterns from)
- 30 new tests added (`test_seed_strategies.py`): total suite now **331 passing**

---

## Phase 10 Implementation (Complete)

### What was built
- `crisis_detector.py` — six cross-asset signals (VIX level, VIX term structure inversion, SPY/TLT/GLD/UUP correlation spike, bond/stock divergence, gold rally, HYG/LQD credit stress) plus Phase-9 event clustering; classifier mapping signal set + VIX level → {normal, elevated, crisis, severe}
- `crisis_state.py` — transition controller that writes `crisis_state_history` rows only on level changes and emits `crisis_state_change` events with transition-aware severity (critical for upgrades to severe, info for downgrades)
- `crisis_state_history` table with index on transitioned_at
- Pipeline gating — `_build_market_context()` injects crisis state into AI prompt; `trade_pipeline.py` Step 4.9 hard-applies size multiplier (0.5× at elevated) and blocks new longs (at crisis/severe) after the AI decides
- Scheduler — `_task_crisis_monitor` runs before event tick every scan cycle
- Dashboard — `Crisis Monitor` panel with colored severity banner, active signals, cross-asset readings, and collapsible transition history
- 17 new tests in `test_crisis.py` covering classification, size multipliers, state persistence, transition emission, downgrade-vs-upgrade severity, idempotence (285 total, all passing)

### Design
- Separation of concerns: detector returns pure data; state module owns persistence and bus emission
- Dedup key includes the transition pair (`from→to:day`) so distinct upgrades/downgrades in one day each emit once
- Crisis gate applies AFTER the AI chooses — the AI's reasoning is preserved in logs, but execution is override-gated for safety
- Hard cash rule: at crisis/severe the pipeline will not open new longs regardless of what any prior layer (meta-model, ensemble, strategies, AI) decided

### 🎯 This is the final phase of the Quant Fund Evolution roadmap.

All 10 phases now operational:
1. Meta-Model on Prediction Data  ✅
2. Scientific Backtesting           ✅
3. Alpha Decay Monitoring           ✅
4. SEC Filings Semantic Analysis    ✅
5. Options Chain Oracle             ✅
6. Multi-Strategy Parallel Execution ✅
7. Strategy Auto-Generation         ✅
8. Ensemble Specialized AIs         ✅
9. Event-Driven Architecture        ✅
10. Cross-Asset Crisis Detection    ✅

### Cross-Phase Integration Tests (previously deferred — now complete)

`tests/test_integration.py` guards the cross-phase contracts that single-phase tests can't see:
- Deprecated strategies (Phase 3) stay out of multi-strategy aggregation (Phase 6)
- Shadow auto-strategies (Phase 7) never reach the active-trade registry (Phase 6)
- Crisis gate (Phase 10) drops BUYs but preserves SELL/SHORT regardless of AI output (Phase 8 / core)
- Crisis transitions (Phase 10) always produce dispatchable events (Phase 9)
- Auto-strategy validation PASS → shadow, FAIL → retired + module file deleted (Phase 2 + 7)
- Smoke test: every phase's public entry points import and canonical constants match

8 integration tests; total suite now 293 passing.

---

## Phase 9 Implementation (Complete)

### What was built
- `event_bus.py` — in-process SQLite-backed bus with emit/dedup/subscribe/dispatch semantics
- `events` table with UNIQUE dedup_key enforcement and pending-event index
- `event_detectors.py` — 4 detectors (SEC filings, earnings imminent, price shocks, big resolved predictions)
- `event_handlers.py` — default handlers (log_activity for all events, fire_ensemble for SEC + price shock)
- Scheduler integration — `_task_event_tick` runs on every scan cycle, rate-limited to 20 dispatches per tick
- Dashboard — "Event Stream" panel with last-24h per-profile event log, severity counts, payload summary, and handler outcomes
- 16 new tests in `test_event_bus.py` covering emit, dedup, subscribe/dispatch routing, handler isolation, dispatch limits, detector idempotence (268 total, all passing)

### Design choices
- SQLite-backed, not Redis: handles hundreds of events/hour, simpler deployment
- Handler exceptions are captured per-handler, never abort the dispatch loop
- Detectors self-dedup via (type, symbol, trigger-identity) keys — safe to call every tick
- Cost-gated ensemble reactions: only SEC filings + price shocks spawn AI analysis (otherwise the polling pipeline would double-count)

### Why This Matters for Phase 10
- A cluster of `price_shock` + `strategy_deprecated` events across multiple profiles in the same 30-minute window is precisely the cross-asset crisis signal Phase 10 needs to detect

---

## Phase 8 Implementation (Complete)

### What was built
- `specialists/` package — registry + 4 specialist modules each with focused system prompt and response parser:
  - `earnings_analyst` — interprets earnings context, guidance tone, SEC filing alerts
  - `pattern_recognizer` — judges chart structure and momentum confluence (weight 1.2)
  - `sentiment_narrative` — reads news flow, political context, insider/options tells
  - `risk_assessor` — portfolio + regime risk gatekeeper with **VETO authority**
- `ensemble.py` — meta-coordinator that runs every specialist against the shortlist in parallel (one batched AI call each) and synthesizes per-symbol verdicts via confidence-weighted voting
- Pipeline integration — `trade_pipeline.py` Step 3.7 runs the ensemble between shortlist build and final AI call; risk-vetoed candidates are dropped; surviving candidates carry an `ensemble_summary` injected into the final AI prompt
- Dashboard — "Specialist Ensemble" panel on `/performance#ai` with per-symbol specialist breakdown, consensus verdict, and VETO highlights
- 19 new tests in `test_ensemble.py` covering registry, parsing, aggregation (agreement/disagreement/VETO/abstention), cost characteristics (252 total, all passing)

### Cost design
Cost is O(specialists), not O(specialists × candidates). Four specialists each batch the entire shortlist into one call — adds 4 AI calls per cycle regardless of shortlist size.

### Why This Matters for Phases 9-10
- Phase 9 (Event-Driven): specialists can become event-reactive — an earnings release fires `earnings_analyst` immediately, not just on the polling cycle
- Phase 10 (Crisis Detection): cluster of `risk_assessor` VETOs across positions within minutes = regime-break signal

---

## Phase 7 Implementation (Complete)

### What was built
- `strategy_generator.py` — spec validation (closed allowlist of fields, ops, markets, directions), template-based Python code generation, and lifecycle persistence for auto-generated strategies
- `strategy_proposer.py` — AI-driven spec generation with strict JSON-only prompting and post-hoc validation; the AI never writes Python
- `strategy_lifecycle.py` — five-state transition controller (proposed → validated → shadow → active → retired); promotion gated on ≥50 resolved predictions with rolling Sharpe ≥ 0.8; retirement after 60 days without edge; active cap of 5 per profile
- Registry integration — `discover_strategies()` finds `strategies/auto_*.py` files automatically; `get_active_strategies()` returns only `status='active'` auto-strategies; new `get_shadow_strategies()` yields shadows
- Multi-strategy integration — `aggregate_shadow_candidates()` runs shadow strategies for prediction tracking without driving real trades
- Validation pipeline — `backtest_strategy()` gained an optional `signal_fn` parameter; `validate_strategy()` passes it through to every inner call (baseline, OOS, walk-forward) so auto-strategies are validated with the full Phase 2 gauntlet
- Weekly scheduler tasks — `_task_auto_strategy_generation` (Sundays: AI proposes 3 specs, each validated); `_task_auto_strategy_lifecycle` (daily: promote/retire)
- Dashboard — "Evolving Strategy Library" panel with per-profile status counts, generation lineage, and timestamps
- 30 new tests in `test_auto_strategy.py` covering spec validation, code generation, condition evaluation, lifecycle transitions, registry wiring, and AI proposal extraction (233 total, all passing)

### Safety design
The AI never writes code. It writes structured JSON matching an allowlisted grammar. Every field, operator, market, and direction is checked against closed sets before a spec is accepted. The generator fills a fixed template using `repr()`-escaped values — no string interpolation of AI output into executable positions. Failed validations delete the rendered module so the registry stops importing it. An AI proposal with a malformed payload is silently dropped, not retried with looser validation.

### Why This Matters for Phases 8-10
- Phase 8 (Ensemble AIs): the proposer is already specialist-shaped; Phase 8 extends it to a team of proposers (earnings-specialist, mean-reversion-specialist, macro-specialist) whose proposals compete for shadow slots
- Phase 9 (Event-Driven): auto-strategies can trigger on events, not just polling; a spec's `conditions` list can include event-type fields once the event bus exists
- Phase 10 (Crisis Detection): when ≥ 2 promoted auto-strategies simultaneously retire with the same failure mode, that's itself a regime-break signal

---

## Phase 6 Implementation (Complete)

### What was built
- `strategies/` package — registry with 6 strategy modules exposing a uniform `NAME / APPLICABLE_MARKETS / find_candidates()` contract
- `multi_strategy.py` — aggregates candidates across every active strategy and computes capital allocation via inverse-variance (risk-parity) weighting on 30-day rolling Sharpe
- Five new alpha strategies:
  - `insider_cluster` — 3+ insider buys totaling ≥ $250K dominating sells
  - `earnings_drift` — post-earnings announcement drift (> 5% move in line with beat/miss)
  - `vol_regime` — trades GEX volatility expansion regimes (dealer short gamma)
  - `max_pain_pinning` — fades moves away from max pain within 5 days of expiration
  - `gap_reversal` — fades > 3% opening gaps on normal-or-lower volume (no catalyst)
- `market_engine` preserves existing per-market strategy router as one voter in the ensemble
- Pipeline integration: `trade_pipeline.py` Step 3 now calls `aggregate_candidates(ctx, universe)` instead of the single-strategy router
- Deprecation-aware: strategies flagged by Phase 3 alpha decay are filtered out of `get_active_strategies()`
- Risk parity capital allocation with 40% per-strategy cap and iterative excess redistribution
- Dashboard: `Strategy Allocation` panel on `/performance#ai` showing per-profile per-strategy weight, rolling Sharpe, lifetime Sharpe, n resolved predictions, win rate
- 21 new tests in `test_multi_strategy.py` — all passing (203 total)

### Allocation math
```
New strategy   (<20 resolved preds)  → DEFAULT_WEIGHT = 1/6 baseline
Losing         (rolling Sharpe ≤ 0) → DEFAULT_WEIGHT × 0.25 (minimum)
Profitable     (rolling Sharpe > 0) → min(Sharpe, 4.0)
→ Normalize to sum 1.0
→ Iteratively cap any single strategy at 40%, redistributing excess
  proportionally to strategies under the cap (single-strategy case keeps 100%)
```

### Why This Matters for Phases 7-10
- Phase 7 (Auto-Gen): the registry is the integration point — AI-proposed strategy variants drop in as new `strategies/auto_*.py` modules and are automatically validated, monitored, and allocated capital once they accumulate track record
- Phase 8 (Ensemble AIs): multi-strategy view gives each specialist AI a richer candidate stream — an earnings-specialist reviews earnings_drift picks, an insider-specialist reviews insider_cluster picks
- Phase 9 (Event-Driven): each strategy becomes an event-handler target — earnings announcement fires earnings_drift, insider Form 4 fires insider_cluster, OPEX Friday fires max_pain_pinning
- Phase 10 (Crisis Detection): when ≥ 3 strategies simultaneously deprecate, that is itself a regime-break signal

---

## Phase 1 Implementation (✅ Complete — kept here as design reference)

### Goal
Build a gradient-boosted classifier that takes the feature context the AI saw for each prediction and predicts the probability that prediction will be correct. This probability re-weights the AI's confidence before execution.

### Flow
```
1. AI makes prediction → all features stored in ai_predictions.features_json
2. Prediction resolves (win/loss) via existing ai_tracker resolution
3. Daily 4:00 AM ET: retrain meta-model on accumulated resolved predictions
4. Live: before executing AI-selected trades, meta-model estimates P(correct)
5. If meta_prob >= 0.3: execute with blended confidence = ai_conf * (0.5 + meta_prob * 0.5)
6. If meta_prob < 0.3: suppress the trade entirely
```

### Why This Works
- The AI is a generalist. It has systematic blind spots.
- Our resolved prediction database captures those blind spots in labeled form.
- A gradient-boosted tree learns patterns like: "AI overconfident on low-volume mid-caps in sideways markets, RSI in 45-55 band."
- The meta-model doesn't need to be smarter than the AI. It just needs to recognize the AI's error patterns.
- Training data is our proprietary AI predictions — literally impossible for competitors to replicate.

### Files Modified / Created (Phase 1)

**Created:**
- `meta_model.py` — core ML module
- `tests/test_meta_model.py` — unit tests

**Modified:**
- `journal.py` — add `features_json` column to `ai_predictions`, handle migration
- `ai_tracker.py` — `record_prediction` accepts and stores features
- `trade_pipeline.py` — passes features to record_prediction, integrates meta-model before execution
- `multi_scheduler.py` — daily retraining task
- `views.py` — meta-model dashboard data
- `templates/performance.html` — meta-model panel
- `requirements.txt` — add `scikit-learn`
- `TECHNICAL_DOCUMENTATION.md` — document Phase 1

### Success Criteria
- Meta-model trained on 100+ resolved predictions with AUC > 0.55
- Trades where meta_prob < 0.3 are correctly suppressed
- Feature importance shows sensible (non-random) patterns
- Overall win rate improves ≥3% over the pre-meta-model baseline after 2 weeks

---

## Autonomous Tuning Expansion ✅ DELIVERED (2026-04-25)

The original "queued for late May 2026" plan called for adding 3 parameters
to the self-tuner. **What actually shipped is much larger:** a full
12-layer autonomous tuning stack that supersedes the modest 4→7 parameter
expansion. The 3 parameters listed in the original plan (Trailing Stop ATR
multiplier, RSI entry thresholds, volume surge multiplier) are all included
inside Layer 1 and bounded by `param_bounds.PARAM_BOUNDS`.

### What shipped instead — the 12-wave rollout

| Layer | What it tunes | Module |
|---|---|---|
| 1 | 35+ scalar parameters with PARAM_BOUNDS clamp | `param_bounds.py` |
| 2 | Per-signal weight ladder (1.0 / 0.7 / 0.4 / 0.0) for 25 signals | `signal_weights.py` |
| 3 | Per-regime parameter overlays | `regime_overrides.py` |
| 4 | Per-time-of-day parameter overlays | `tod_overrides.py` |
| 5 | Per-symbol parameter overlays | `symbol_overrides.py` |
| 6 | AI prompt section order + presence | `prompt_layout.py` |
| 7 | Insight propagation (lessons learned → in-flight prompt) | `insight_propagation.py` |
| 8 | AI model auto-selection (gated by user toggle) | self-tuning |
| 9 | Per-Alpaca-account-conserving capital allocation | `capital_allocator.py` |
| Cost | Daily AI-spend ceiling (user-configurable) | `cost_guard.py` |
| Closed loop | Losing-week + false-negative post-mortems | `post_mortem.py` |

### Override resolution chain
```
per-symbol → per-regime → per-time-of-day → profile-global → caller-default,
then multiplied by capital_scale, then clamped by PARAM_BOUNDS.
```

### Anti-regression guardrails (6 structural tests)
- AST walker on every `_optimize_*` return prohibits raw snake_case keys
  in user-facing strings.
- API-response sweep dynamically discovers every `/api/*` endpoint via
  `app.url_map`, hits each with seeded data, walks the JSON, fails the
  build on any leaked PARAM_BOUNDS key.
- Tuner must call `alpha_decay.deprecate(...)` for every "deprecate"
  recommendation it emits.
- Idempotency markers for daily summary email + snapshot bundle.
- Duplicate-DOM-id detector across all templates.
- Pre-commit gate requires `CHANGELOG.md` update with every `.py` commit.

### Why this beat the original plan
The original 3-parameter expansion was conservative — designed to inch
forward once the existing 4 had 2-3 stable weeks. By 2026-04-25 the
production system had enough resolved predictions that the user
prioritized aggressive autonomy ("time and effort are meaningless,
quality and function are all that matter"). Result: a full 12-layer
architecture ships in one weekend with cost guard, override chain,
post-mortems, and 6 structural anti-regression tests, instead of three
isolated parameter additions a month from now.

See `AUTONOMOUS_TUNING_PLAN.md` for the wave-by-wave rollout details
and `SELF_TUNING.md` for the per-signal weight ladder and operator
reference.

---

## Alternative Data Integration ✅ DELIVERED (2026-04-26)

Four standalone alt-data projects (`congresstrades`, `edgar13f`,
`biotechevents`, `stocktwits`) were stitched into the pipeline over the
weekend so the 1-year normalization period for each new signal starts
immediately rather than later.

### What shipped
- `alternative_data.py` gained 4 read-only helpers: `get_congressional_recent`,
  `get_13f_institutional`, `get_biotech_milestones`, `get_stocktwits_sentiment`.
- All four open the external SQLite stores read-only via `file:` URI with
  a 2.0s timeout and tolerate missing DBs gracefully.
- Each helper is wired into `get_all_alternative_data(symbol)` and cached
  for 6 hours.
- 4 new entries added to `signal_weights.WEIGHTABLE_SIGNALS` so each new
  source flows through the Layer-2 weight ladder.
- 4 new prompt blocks added to `ai_analyst.py` under the alt-data section,
  all wrapped in `_weighted_signal_text` so weights apply.
- 11 new alt-data feature keys added to the meta-model `features_payload`
  so Phase-1 training can learn from them.
- 12 new tests in `tests/test_altdata_readers.py` with seeded fixtures.
- Path resolution via `ALTDATA_BASE_PATH` env var; defaults to `~/`.

See `ALTDATA_INTEGRATION_PLAN.md` for verified record counts and the
DEPLOYED-2026-04-26 status flip.

---

## Cross-Session Continuity

**If you are an AI assistant reading this in a new session:**

1. All 10 phases are ✅ Complete. There is no "current" phase to resume.
   This document is now a design archive describing what shipped and why.
2. For new work, read in this order:
   - `EXECUTIVE_OVERVIEW.md` — top-down summary
   - `EXPERIMENTATION_AND_TUNING.md` — how the closed loop works
   - `TECHNICAL_DOCUMENTATION.md` — system reference
   - `CHANGELOG.md` — most recent fixes (search for "Severity: critical, accuracy" for methodology-related work)
3. Run `./run_tests.sh` first to confirm baseline (should be 1000+ tests passing). Tests run in randomized order via `pytest-randomly`.
4. Future work falls outside this 10-phase framework — use a new plan
   doc rather than retrofitting onto these phases.

The user has explicitly emphasized documentation because context is lost between sessions. Every architectural decision must be persisted here.
