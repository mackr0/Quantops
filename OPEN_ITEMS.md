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
| **MC bootstrap by-day not by-trade** | ✅ DONE 2026-05-03 | `bootstrap_mode='by_day'` is the default at `mc_backtest.py:128`; samples one slippage realization per day so correlated regimes (full day of wide spreads) are captured. `per_trade` mode kept as a legacy baseline. |
| **ADV-at-trade-time storage** | ✅ DONE | `adv_at_decision` column captured at order submit; calibrator at `slippage_model.py:163-204` uses real participation rate (`qty / adv_shares`) instead of the coarse $50M proxy. Legacy rows pre-dating the column fall back to the proxy. |
| **Slippage model recalibration after real money** | 🔒 DEFERRED | K is currently fitted from paper fills (see `slippage_model.py:42` docstring); rerun after 30+ days live. |

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
| Phase F2. Macro event plays (FOMC/CPI/NFP) | ✅ DONE | Tracker shipped 2026-05-03; integration shipped 2026-05-09 (`render_macro_play_recommendation_for_prompt` + trade_pipeline + ai_analyst wiring) |
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
| W3. Production deployment (`altdata/` subdirectory after 2026-05-04 merge into main repo; was `/opt/quantopsai-altdata/`) | ✅ DONE |
| W4. UI + docs | ✅ DONE |
| **PDUFA scraper** | ✅ DONE 2026-05-04 (commits `ffe8b9c..41c3b28`). EDGAR full-text search for "PDUFA date" in 8-K filings; populates 10/10 events with real drug names + action types after the regex iteration. |

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
| `alternative_data.py` (App Store WoW) | Earlier "leave None — future enhancement when daily snapshots persist" | ✅ DONE — WoW logic implemented at `alternative_data.py:2018-2096` (`_get_wow_change`, "Item 2 of OPEN_ITEMS — WoW change vs 7 days ago") |
| `mc_backtest.py` (by-day bootstrap) | Earlier "future enhancement" framing | ✅ DONE 2026-05-03 — `bootstrap_mode='by_day'` is the default (line 128); module docstring rewritten in Issue 10 (commit `47de74d`) |
| `multi_scheduler.py:1257-1284` | Earlier `multi_scheduler.py:1196` "sector_moves + halted_held_symbols deferred" | ✅ DONE 2026-05-09 — comment removed; `_compute_sector_moves` (L1257) + `_compute_halted_held_symbols` (L1284) wired into the intraday risk check; AST guardrail in `tests/test_intraday_risk_full_wiring.py` enforces all kwargs are passed |
| `options_earnings_plays.py:24-26` | Earlier `:25` "with index ETFs (SPY/QQQ); deferred until macro-event tracker exists" | ✅ DONE 2026-05-09 — comment rewritten to point at the macro analog (`macro_event_tracker.render_macro_play_recommendation_for_prompt`) which is wired in trade_pipeline + ai_analyst |
| `options_roll_manager.py:31-34` | Earlier "Roll-window thresholds. Tunable per-profile in a future commit." | ✅ DONE — comment now reads "these are now per-profile tunable knobs (UserContext fields, settings UI). Module constants stay as fallbacks when a function is called without ctx." |
| `slippage_model.py:163-168` | Earlier `:165` "We don't store ADV at trade time, so use a simple proxy" | ✅ DONE 2026-05-10 — comment rewritten in Issue 10 (commit `47de74d`) to describe actual behavior (`adv_at_decision` IS stored and used; legacy rows fall back to the $50M ADV proxy) |
| `slippage_model.py:42` | Earlier `:197` "K is currently fitted from paper fills" — text now lives in module docstring at L42: "fills will deviate; the calibrator should be re-run after going [live]" | 🔒 DEFERRED — recalibrate after real money. Concept unchanged; only the line moved. |
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
| Slippage MC IID per trade — RESOLVED via `bootstrap_mode='by_day'` (default) at `mc_backtest.py:128` | — | Kept here as a historical limit note; the per_trade mode is preserved as a legacy baseline. |
| Slippage K calibrated from paper | `slippage_model.py:42` | Real-money fills will deviate. K refit deferred until 30+ days live. |

---

## 12. Recommended next batch — STATUS

All 10 items SHIPPED 2026-05-03. Commits: `91a6f9a` (#1-4), `81d4d95` (#5-10).

1. ✅ **ADV-at-trade-time storage** — `trades.adv_at_decision` captured at submit; slippage calibrator uses real participation_rate.
2. ✅ **App Store WoW snapshot task** — `app_store_history` table, daily-idempotent snapshot, WoW deltas in prompt.
3. ✅ **MC bootstrap by-day** — `bootstrap_mode='by_day'` default; whole-day slippage realizations shared across same-day trades.
4. ✅ **`wheel_symbols` settings UI** — schema column + textarea + parser.
5. ✅ **Options backtester dashboard panel** — `/api/options-backtest` + Run button on Brain tab.
6. ✅ **PDUFA scraper** — `pdufa_scraper.py` BiopharmCatalyst scrape + daily-idempotent task.
7. ✅ **Short borrow rate tracking** — 3-tier rate lookup (HTB / non-GC / GC) + per-candidate annotation.
8. ✅ **AI vocabulary for proposing options trades** — OPTIONS action ungated for any candidate with `options_oracle_summary`.
9. ✅ **Macro event tracker (Phase F2)** — `macro_event_tracker.py` with FOMC/CPI/NFP calendar + evaluate_macro_play.
10. ✅ **Per-profile options roll-window knobs** — 3 schema columns; `evaluate_for_roll` parameterized; settings UI.

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
