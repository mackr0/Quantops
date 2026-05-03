# QuantOpsAI — Open Items (Master List)

**Date:** 2026-05-03
**Purpose:** Single source of truth for every open / deferred / partial item across every plan file in the repo and every code-level marker (`TODO`, `deferred`, `future enhancement`, `honest limit`). One place to look so nothing stays invisible.

**How to read it:**
- ✅ DONE — fully shipped + verified
- ⚠ PARTIAL — substantially shipped with named gaps
- ⏳ OPEN (free) — buildable now, no paid dependency
- 💰 OPEN (paid) — requires a paid feed / account / vendor
- 🔒 DEFERRED — explicitly out-of-scope for now (real-money phase, etc.)

**Rule for keeping this current:**
When something here moves to ✅, update the entry with the commit + date. When new work surfaces a new gap, add an entry. The CHANGELOG tracks history; this file tracks what's still pending.

---

## 1. COMPETITIVE_GAP_PLAN.md

| Item | Status | Notes |
|---|---|---|
| 1a. Options trading layer | ✅ DONE | Phases A-F + H1-H4 of OPTIONS_PROGRAM_PLAN |
| 1b. Statistical arbitrage at scale | ✅ DONE | `stat_arb_pair_book.py` + scheduler tasks (gated `enable_stat_arb_pairs`) |
| 1c. Volatility strategies | ⚠ PARTIAL | Phases E/F shipped (vol regime, earnings IV crush). Long-vol portfolio hedge ✅ DONE 2026-05-02 (`long_vol_hedge.py`) |
| 2a. Barra-style multi-factor model | ✅ DONE | `portfolio_risk_model.py` + `risk_stress_scenarios.py` |
| 2b. Intraday risk monitoring | ✅ DONE | `intraday_risk_monitor.py`, gated `enable_intraday_risk_halt` |
| 3a. Web-scraped alt data | ⚠ PARTIAL | See §1.1 below |
| 3b. Earnings-call sentiment NLP | ✅ DONE | `sec_filings.get_earnings_call_sentiment` |
| 3c. Paid data feeds | 💰 OPEN | Quiver Quant ($30-100/mo), Polygon ($50/mo), Benzinga Pro ($150/mo) |
| 4a. Futures + FX via IBKR | ⏳ OPEN | ~1 month build; opens cross-asset hedging |
| 4b. Crypto deeper build | 🔒 DEFERRED | Awaiting strategy thesis |
| 5a. Online / continuous learning | ✅ DONE | `online_meta_model.py` (SGD freshness layer) |
| 5b. Adversarial / red-team specialist | ✅ DONE | 5th specialist with VETO authority |
| 5c. Better backtesting infrastructure | ⚠ PARTIAL | See §1.2 below |
| 6a. Real money via IBKR Pro | ⏳ OPEN | Within 4a; ready once Alpaca paper proves out |
| 6b. Capital allocation across strategies | ✅ DONE | `strategy_capital_allocator.py` |

### 1.1 Open inside 3a (web-scraped alt data)

| Sub-item | Status | Effort |
|---|---|---|
| Reddit ticker mentions | ✅ DONE | `social_sentiment.get_ticker_mentions` |
| StockTwits sentiment | ✅ DONE | `alternative_data.get_stocktwits_sentiment` |
| Earnings transcript NLP | ✅ DONE | `sec_filings.get_earnings_call_sentiment` |
| Congressional trades | ✅ DONE | `alternative_data.get_congressional_recent` |
| Institutional 13F holdings | ✅ DONE | `alternative_data.get_13f_institutional` |
| Biotech FDA / PDUFA milestones | ⚠ PARTIAL | `alternative_data.get_biotech_milestones` works for clinical trials; **PDUFA scraper deferred** (per ALTDATA_INTEGRATION_PLAN.md line 11: "0 PDUFA events"). |
| Google Trends search interest | ✅ DONE | `alternative_data.get_google_trends_signal` |
| Wikipedia page-views | ✅ DONE | `alternative_data.get_wikipedia_pageviews_signal` |
| App Store rankings | ⚠ PARTIAL | `alternative_data.get_app_store_ranking` shipped; **WoW change is None** (no daily snapshot). Fix: daily snapshot task → compute delta. |
| GitHub commit activity | 🔒 DEFERRED | Most S&P doesn't have meaningful public repos; weak signal. |
| Job-postings volume | 🔒 DEFERRED | No clean free source (LinkedIn paid, Indeed TOS-fragile). |
| 10b5-1 insider planned-sale tracking | ⏳ OPEN | More granular than current insider data; SEC EDGAR free. |

### 1.2 Open inside 5c (better backtesting)

| Sub-item | Status |
|---|---|
| Walk-forward + OOS-disjoint splits in `rigorous_backtest` | ✅ DONE |
| Synthetic options backtester (Phase H of options plan) | ✅ DONE |
| Realistic slippage model (`slippage_model.py`) | ✅ DONE |
| Monte Carlo backtest with bootstrap (`mc_backtest.py`) | ✅ DONE |
| Per-strategy MC tiles | ✅ DONE 2026-05-03 |
| **MC bootstrap by-day not by-trade** | ⏳ OPEN | Currently IID per trade; doesn't capture correlated regimes (full day of wide spreads). Code limit documented in `mc_backtest.py:25`. |
| **ADV-at-trade-time storage** | ⏳ OPEN | Slippage K calibration uses a coarse `$50M default ADV`. Add `adv_at_decision` column to trades + capture at submit. Calibration becomes much more accurate. |
| **Slippage model recalibration after real money** | 🔒 DEFERRED | K is currently fitted from paper fills; rerun after 30+ days live. |

---

## 2. OPTIONS_PROGRAM_PLAN.md

| Item | Status | Notes |
|---|---|---|
| Phase A. Greeks (aggregator + gates + dashboard) | ✅ DONE | A1-A3 |
| Phase B. Multi-leg primitives + atomic execution | ✅ DONE | B1-B4, 11 builders |
| Phase C. Lifecycle (roll + assignment + wheel) | ✅ DONE | C1-C3 |
| Phase D. Dynamic delta hedging | ✅ DONE | D1 |
| Phase E. Vol surface analysis | ✅ DONE | E1-E4 |
| Phase F1. Earnings vol plays | ✅ DONE | |
| Phase F2. Macro event plays (FOMC/CPI/NFP) | ⏳ OPEN | Per `options_earnings_plays.py:25`: "deferred until macro-event tracker exists" |
| Phase G1. Real-time options chain feed | 🔒 DEFERRED | "Defer until real-money phase" (per plan) |
| Phase H1. Synthetic options backtester L1-L4 | ✅ DONE | 31 tests |
| **Phase H L5. Backtester dashboard integration** | ⏳ OPEN | API callable; UI panel not yet wired. Plan flags as "not strictly needed". |
| **`wheel_symbols` populated per profile** | ⏳ OPEN | `options_wheel.py` is built but NO profile has the field set, so wheel never fires. Need: settings UI + per-profile opt-in symbol list. |

---

## 3. ROADMAP.md (10-phase main + Phases 11-13)

| Phase | Status |
|---|---|
| 1. Meta-model on own predictions | ✅ DONE |
| 2. Scientific backtesting infra (10 gates) | ✅ DONE |
| 3. Alpha decay monitoring | ✅ DONE |
| 4. SEC filings semantic analysis | ✅ DONE |
| 5. Options chain oracle | ✅ DONE |
| 6. Multi-strategy parallel execution | ✅ DONE |
| 7. Strategy auto-generation | ✅ DONE |
| 8. Ensemble of specialized AIs | ✅ DONE |
| 9. Event-driven architecture | ✅ DONE |
| 10. Cross-asset crisis detection | ✅ DONE |
| 11. Long/Short parity | ✅ DONE | Phases 1-4 of LONG_SHORT_PLAN |
| 12. Exit execution hardening | ✅ DONE | All 4 stages of INTRADAY_STOPS_PLAN |
| 13. Competitive-gap closure | ⚠ PARTIAL | See §1 above |

---

## 4. LONG_SHORT_PLAN.md

| Phase | Status |
|---|---|
| Phase 1 (1.0 → 1.14) | ✅ DONE |
| Phase 2 (2.1 → 2.5) | ✅ DONE |
| Phase 3 (3.1 → 3.6) | ✅ DONE |
| Phase 4 (4.1 → 4.5) | ✅ DONE |

Nothing open in this plan.

---

## 5. INTRADAY_STOPS_PLAN.md

| Stage | Status | Commit |
|---|---|---|
| Stage 1: Static stop-loss on entry | ✅ DONE | 3d84543 |
| Stage 2: Take-profit (replaced by Stage 3) | ✅ DONE | b024ab8 (superseded) |
| Stage 3: Trailing-stop on entry | ✅ DONE | f34b81f |
| Stage 4: Polling defers to broker | ✅ DONE | 7dbbf88 |

Nothing open in this plan.

---

## 6. COST_AND_QUALITY_LEVERS_PLAN.md

| Lever | Status |
|---|---|
| 1. Persistent disk cache for ensemble + political_context | ✅ DONE |
| 2. Meta-model pre-gate before ensemble | ✅ DONE |
| 3. Per-profile specialist disable list (auto-disable + auto-re-enable) | ✅ DONE |

Nothing open in this plan.

---

## 7. ALTDATA_INTEGRATION_PLAN.md

| Wave | Status |
|---|---|
| W1. Read layer (4 helpers) | ✅ DONE |
| W2. AI integration | ✅ DONE |
| W3. Production deployment (`/opt/quantopsai-altdata/`) | ✅ DONE |
| W4. UI + docs | ✅ DONE |
| **PDUFA scraper** | ⏳ OPEN | Plan acceptance criteria call out "0 PDUFA events"; scraper still deferred. Clinical trials part works. |

---

## 8. DYNAMIC_UNIVERSE_PLAN.md

| Step | Status |
|---|---|
| 1. Sector classification module (`sector_classifier.py`) | ✅ DONE |
| 2. Historical-universe freeze (`segments_historical.py`) | ✅ DONE |
| 3. Dynamic universe provider in `segments.py` | ✅ DONE |
| 4. Remove `screener.py` dead weight | ✅ DONE |
| 5. UI updates (`views.py`) | ✅ DONE |
| 6. Tests | ✅ DONE |
| 7. CHANGELOG | ✅ DONE |
| 8. Deploy + verify | ✅ DONE |

Out-of-scope (per plan §7): multi-exchange expansion, corporate-action awareness, crypto dynamic discovery, short-availability tracking — all 🔒 DEFERRED by design.

---

## 9. SCALING_PLAN.md (graduation milestones)

| Stage | Capital | Status |
|---|---|---|
| Stage 1: $10K Paper | $10K | ✅ ACTIVE |
| Stage 2: $10K Real Money | $10K | ⏳ OPEN | Prerequisite: Stage 1 success criteria (30+ days, >45% win rate). Switch Alpaca paper → live. |
| Stage 3: $50K Real Money | $50K | ⏳ OPEN | Prerequisites: Stage 2 profitable 60+ days. Add Polygon real-time data, $5M ADV filter, limit orders by default. |
| Stage 4: $100K-$250K | $100K+ | ⏳ OPEN | WebSocket streaming arch, Level 2 order book, VWAP execution, iceberg orders. |
| Stage 5: $1M+ | $1M+ | ⏳ OPEN | Full execution rebuild, dedicated infra, regulatory compliance. |

---

## 10. Code-level markers (`grep` of `.py` for TODO / deferred / future enhancement)

| File:line | Item | Status |
|---|---|---|
| `ai_analyst.py:640` | "the AI to propose with action='OPTIONS' (deferred to follow-up)" | ⏳ OPEN — surface vocabulary for AI to propose options trades directly |
| `alternative_data.py:1928` | App Store WoW rank change — "leave None — future enhancement when daily snapshots persist" | ⏳ OPEN — covered above in §1.1 |
| `mc_backtest.py:25` | "correlated regimes... To capture those, we'd need to bootstrap by day, not by trade — future enhancement" | ⏳ OPEN — covered above in §1.2 |
| `multi_scheduler.py:1196` | "sector_moves + halted_held_symbols deferred" | ⏳ OPEN — intraday risk monitor accepts these but scheduler isn't computing them yet |
| `options_earnings_plays.py:25` | "with index ETFs (SPY/QQQ); deferred until macro-event tracker exists" | ⏳ OPEN — covered as Phase F2 in §2 |
| `options_roll_manager.py:32` | "Roll-window thresholds. Tunable per-profile in a future commit." | ⏳ OPEN — currently module constants, would benefit from per-profile knobs |
| `slippage_model.py:165` | "We don't store ADV at trade time, so use a simple proxy" | ⏳ OPEN — covered above in §1.2 |
| `slippage_model.py:197` | "K is currently fitted from paper fills" | 🔒 DEFERRED — recalibrate after real money |
| `short_borrow.py:3` | "DYNAMIC_UNIVERSE_PLAN.md / TECHNICAL_DOCUMENTATION.md §15 deferred" | ⏳ OPEN — short borrow rate tracking infrastructure (currently uses Alpaca's binary `easy_to_borrow` flag only) |

---

## 11. Documented honest limits (acknowledged but not fixed)

These are NOT bugs; they're scope constraints surfaced in code comments. They shape future work direction.

| Limit | File | Notes |
|---|---|---|
| Synthetic options backtester ≠ precise P&L | `OPTIONS_PROGRAM_PLAN.md` Phase H | Doesn't capture bid-ask spread, IV term structure, catalyst vol pop. Sufficient for STRATEGY VALIDATION, not PRECISE FORECASTING. |
| Parametric VaR understates tails | `portfolio_risk_model.py` | Assumes normal returns; Monte Carlo helps but inherits factor distribution normality. |
| Stress scenarios miss cross-asset risk | `risk_stress_scenarios.py` | No rates / FX / commodities in factor set yet. 2022-style rate shocks under-report. |
| 1987 / dot-com scenarios use French only | `risk_stress_scenarios.py` | Sector ETFs didn't exist; sector-tilt P&L flagged as "approximation_quality: low" or "medium". |
| Long-vol hedge: SPY puts hedge BETA, not idio | `long_vol_hedge.py` | Concentrated single-name books still bleed even if SPY rallies. |
| Slippage MC: IID per trade | `mc_backtest.py:25` | Doesn't capture full-day-wide-spread regime correlation. |
| Slippage K calibrated from paper | `slippage_model.py:197` | Real-money fills will deviate. |

---

## 12. Recommended next batch (free, high leverage, in priority order)

These are the items that materially improve correctness or visibility WITHOUT requiring paid feeds, IBKR, or real money. Listed roughly in dependency order.

1. **ADV-at-trade-time storage** (½ day). Add `adv_at_decision` column to `trades` table; capture `get_bars(symbol, limit=20)` 20-day average volume at submit time. Slippage `calibrate_from_history` uses real participation instead of `$50M default`. Calibration becomes useful instead of approximate.

2. **App Store WoW snapshot task** (½ day). Daily scheduler task snapshots `get_app_store_ranking(symbol)` for tickers in `APP_STORE_TICKER_OVERRIDES` to a new `app_store_history` table. Enables `wow_change_grossing` / `wow_change_free` deltas — turns flat ranks into a trend signal.

3. **MC bootstrap by-day** (1 day). Refactor `mc_backtest.run_monte_carlo` to bootstrap whole-day slippage realizations instead of per-trade IID. Captures correlated-regime variance that the current model misses.

4. **`wheel_symbols` settings UI** (½ day). Add a multi-select / comma-list field on settings page; populate per-profile so the wheel state machine can fire on chosen names. Currently built but inert.

5. **Phase H L5 — backtester dashboard panel** (1 day). Run-button + result curve for the synthetic options backtester. Currently API-callable only; closes the OPTIONS_PROGRAM_PLAN gap.

6. **PDUFA scraper** (1-2 days). Closes ALTDATA_INTEGRATION_PLAN's last open item — biotech catalyst signal. Source: FDA's PDUFA calendar (free).

7. **Short borrow rate tracking** (1 day). Replace Alpaca's binary `easy_to_borrow` with continuous borrow-rate data (FINRA RegSHO daily file, free). Material on HTB shorts where rate eats 5-50%/yr.

8. **AI vocabulary for proposing options trades** (1-2 days). `ai_analyst.py:640` deferred follow-up — let AI propose `action='OPTIONS'` trades directly with strategy + strike + expiry. Currently options trades are surfaced via the multi-leg advisor; AI can't construct them itself.

9. **Macro event tracker (Phase F2)** (1 day). FOMC / CPI / NFP date list + IV-crush capture on index ETFs. Same template as F1 earnings.

10. **Per-profile options roll-window knobs** (½ day). Module constants in `options_roll_manager.py` → schema columns + settings UI.

---

## 13. NOT pursuing (explicitly not on the open list)

Per `COMPETITIVE_GAP_PLAN.md` §"Explicitly NOT pursuing":
- Latency arbitrage (sub-microsecond + colocation)
- Market making (exchange membership + low-latency infra)
- Block trading capacity
- Index inclusion arbitrage
- Insider-information networks (paid expert networks)

These are real differentiators of billion-dollar funds but the gap is structural, not addressable in software.

---

## How this list is maintained

- **Adding an item:** when a code marker (`TODO`, `deferred`, `future enhancement`) gets shipped, add it under §10 with a status. When a new plan ships with new gaps, add a section.
- **Closing an item:** mark ✅ DONE with the commit + date. Don't delete entries — keeping them visible documents what was completed.
- **Quarterly sweep:** every ~3 months, re-run the `grep` audit (see §10) to catch any new code-level deferrals that snuck in. The pattern matchers: `TODO`, `FIXME`, `HACK`, `XXX`, `deferred`, `defer until`, `future enhancement`, `future:`, `NOT YET`, `not yet built`, `not yet wired`, `future improvement`, `improve later`, `known limit`, `limitation:`, `honest limit`.
