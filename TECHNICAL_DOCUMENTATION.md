# QuantOpsAI — Technical Documentation

**Version:** 5.0
**Date:** April 26, 2026
**System:** AI-powered autonomous paper trading platform
**Architecture:** Python 3.12 / Flask / SQLite / DigitalOcean

---

## Recent Additions Since v4.0 (April 12 → April 26)

This file documents the system's stable architecture. Two large
expansions shipped between v4.0 and v5.0; their canonical references
are in dedicated documents:

- **`AUTONOMOUS_TUNING_PLAN.md`** — the 9-layer autonomous-tuning
  architecture (parameter coverage, weighted signal intensity,
  per-regime / per-time-of-day / per-symbol overrides, cross-profile
  insight propagation, adaptive prompt structure, self-commissioned
  strategies, capital allocation). Cost guard cross-cutting all of it.
- **`SELF_TUNING.md`** — every tuning rule, every signal, every safety
  guardrail in the autonomy system.
- **`ALTDATA_INTEGRATION_PLAN.md`** — the four standalone alt-data
  projects (`congresstrades`, `edgar13f`, `biotechevents`, `stocktwits`)
  deployed to `/opt/quantopsai-altdata/` with daily cron at 06:00 UTC.
- **`AI_ARCHITECTURE.md`** — end-to-end map of every AI agent +
  every feedback loop including the 12 autonomy layers.
- **`LONG_SHORT_PLAN.md`** — Phases 1-4 of the long/short build
  (April 28-29). Real long/short equity capability — bearish
  strategies, sector/factor neutrality, real alpha sources, active
  factor construction (Kelly, drawdown scaling, risk-parity,
  market-neutrality enforcement).

The sections below have been refreshed where needed (Self-Tuning
summary, schema, scheduler, config, codebase). For the deep dive,
follow the canonical docs above.

### Long/short capability modules (April 28-29 expansion)

Beyond the architecture above, the long/short build added these
modules. Each is single-purpose and tested in isolation:

- **`portfolio_exposure.py`** — `compute_exposure` (sector + factor
  + direction breakdown), `compute_book_beta` (gross-weighted),
  `compute_factor_exposure` (size bands, book/value, beta, momentum
  buckets), `find_pair_opportunities` (same-sector long+short),
  `balance_gate` (target_short_pct enforcement), `simulate_book_beta_with_entry`
  (P4.5 neutrality projection), `render_for_prompt` /
  `render_pairs_for_prompt` (AI prompt blocks).
- **`factor_data.py`** — yfinance fundamentals with 7-day cache:
  `get_book_to_market`, `get_beta`, `get_momentum_12_1`, plus
  `get_realized_vol(symbol, days=30)` for risk-parity sizing.
- **`kelly_sizing.py`** — `compute_kelly_fraction(win_rate, avg_win,
  avg_loss, fractional=0.25)` implements `f* = (bp - q) / b × fractional`.
  `compute_kelly_recommendation(db_path, direction)` reads per-direction
  edge stats from `ai_predictions` (filtering HOLD predictions —
  only entry signals BUY/STRONG_BUY for long, SHORT/SELL/STRONG_*
  for short — pollute the win rate otherwise). Surfaces to AI as
  `KELLY SIZING` block.
- **`drawdown_scaling.py`** — continuous capital-scale modifier in
  [0.25, 1.0]. Linear interp between breakpoints (0%→1.00, 5%→0.85,
  10%→0.65, 15%→0.45, 20%+→0.25). Independent of the discrete
  pause/reduce action — scaling shrinks the entries that DO happen.
- **`risk_parity.py`** — risk-budget sizing. `compute_vol_scale(vol,
  target_vol=0.25)` returns `target_vol / vol` clamped to [0.4, 1.6].
  `analyze_position_risk(positions, equity)` flags names whose
  `weight × annualized_vol` is ≥ 2× or ≤ 0.5× the per-position avg.
- **Bearish strategies (10 total).** P1.1's 5 (`breakdown_support`,
  `distribution_at_highs`, `failed_breakout`, `parabolic_exhaustion`,
  `relative_weakness_in_strong_sector`); P3.1-P3.4's 4
  (`earnings_disaster_short`, `catalyst_filing_short`,
  `sector_rotation_short`, `iv_regime_short`); plus
  `relative_weakness_universe` — universe-wide anti-momentum ranker
  added to fill short books in regimes where textbook bearish
  technical patterns are rare.
- **`trade_pipeline._rank_candidates`** — accepts `target_short_pct`.
  When ≥ 0.4, the strong-bull regime gate is bypassed for shorts on
  that profile (mandate = explicit acceptance of regime risk).
- **`ai_analyst._validate_ai_trades` gates** — balance gate (P2.4),
  asymmetric short cap (P1.6), HTB borrow penalty (P1.14), market-
  neutrality gate (P4.5: blocks entries that push `|book_beta - target|`
  by >0.5).
- **`client.get_borrow_info`** — Alpaca shortable + easy_to_borrow
  flags with 24h cache, used as quality filter and HTB sizing input.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Architecture](#2-architecture)
3. [Market-Specific Strategy Engines](#3-market-specific-strategy-engines)
4. [AI Analysis Pipeline](#4-ai-analysis-pipeline)
5. [Trade Execution Pipeline](#5-trade-execution-pipeline)
6. [Risk Management](#6-risk-management)
7. [Self-Tuning & Machine Learning](#7-self-tuning--machine-learning)
8. [Intelligence Features](#8-intelligence-features)
9. [Database Schema](#9-database-schema)
10. [Web Application](#10-web-application)
11. [Scheduler & Automation](#11-scheduler--automation)
12. [External Integrations](#12-external-integrations)
13. [Multi-User Security Model](#13-multi-user-security-model)
14. [Configuration Reference](#14-configuration-reference)
15. [Cost Model](#15-cost-model)
16. [Codebase Reference](#16-codebase-reference)

---

## 1. System Overview

QuantOpsAI is an autonomous paper trading system that:

1. **Screens** hundreds of stocks/crypto across 5 market segments using Yahoo Finance data (free)
2. **Analyzes** candidates with market-specific technical strategies (each segment has its own engine)
3. **Reviews** actionable signals through AI (Claude/GPT/Gemini) before any trade executes
4. **Executes** on Alpaca's paper trading platform (commission-free, simulated)
5. **Tracks** every AI prediction and resolves it against actual price movements
6. **Self-tunes** by feeding past performance back into the AI prompt and auto-adjusting parameters
7. **Notifies** users via email for every trade, veto, exit, and daily summary

The system operates 24/7 for crypto and during US market hours for equities, running on a $6/month DigitalOcean droplet.

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    QuantOpsAI Architecture                       │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌──────────┐    ┌──────────────┐    ┌──────────────┐          │
│  │ Flask    │    │ Multi-User   │    │ Strategy     │          │
│  │ Web App  │    │ Scheduler    │    │ Engines (5)  │          │
│  │ (Gunicorn)│    │ (systemd)    │    │              │          │
│  └────┬─────┘    └──────┬───────┘    └──────┬───────┘          │
│       │                 │                    │                   │
│       └────────┬────────┴────────────┬───────┘                  │
│                │                     │                           │
│         ┌──────▼──────┐      ┌──────▼──────┐                   │
│         │   SQLite    │      │   AI Layer  │                   │
│         │  Databases  │      │ (multi-     │                   │
│         │  (per-user/ │      │  provider)  │                   │
│         │   profile)  │      └──────┬──────┘                   │
│         └─────────────┘             │                           │
│                              ┌──────▼──────┐                   │
│                              │  Alpaca API │                   │
│                              │ (paper      │                   │
│                              │  trading)   │                   │
│                              └─────────────┘                   │
├─────────────────────────────────────────────────────────────────┤
│  Data Sources: Yahoo Finance (free) | RSS Feeds (free)          │
│  AI Providers: Anthropic | OpenAI | Google (user's API keys)    │
│  Notifications: Resend Email API                                │
│  Infrastructure: DigitalOcean droplet ($6/mo), nginx, systemd   │
└─────────────────────────────────────────────────────────────────┘
```

### Component Summary

| Component | Technology | Purpose |
|---|---|---|
| Web App | Flask + Gunicorn + nginx | User interface, settings, dashboards |
| Scheduler | Python systemd service | Autonomous trading loop |
| Strategy Engines | 5 Python modules | Market-specific signal generation |
| AI Layer | Anthropic/OpenAI/Google APIs | Trade validation and consensus |
| Database | SQLite (WAL mode) | User data, trades, predictions, tuning history |
| Market Data | Yahoo Finance (yfinance) | OHLCV bars, indicators, earnings |
| Execution | Alpaca REST API | Paper trade placement and monitoring |
| Notifications | Resend API | Email alerts for trades, vetoes, summaries |

---

## 3. Market-Specific Strategy Engines

Each market segment has its own strategy engine with strategies designed for that market's specific behavior patterns.

### 3.1 Micro Cap ($1–$5) — `strategy_micro.py`

**Market Characteristics:** Extreme volatility, low liquidity, catalyst-driven, penny stock behavior.

| Strategy | BUY Trigger | EXIT Trigger |
|---|---|---|
| **Volume Explosion** | Volume > 5x 20-day avg AND price up > 5% AND RSI < 75 | Volume drops below 2x avg |
| **Penny Reversal** | RSI < 20 AND price > 20% below 10-day SMA AND volume increasing | Price returns to SMA10 OR RSI > 50 |
| **Breakout Above Resistance** | Price > 10-day high AND volume > 3x avg | Price drops below breakout level |
| **Trap Avoidance Filter** | SKIP if avg volume < 100K or 10+ consecutive down days | — |

**Default Risk:** 10% stop-loss, 15% take-profit, 5% max position size

### 3.2 Small Cap ($5–$20) — `strategy_small.py`

**Market Characteristics:** Volatile but more established, sector-sensitive, mean reversion works.

| Strategy | BUY Trigger | EXIT Trigger |
|---|---|---|
| **Mean Reversion** | RSI < 28 AND price > 8% below 20-day SMA | Price returns to SMA20 OR RSI > 55 |
| **Volume Spike Entry** | Volume > 3x avg AND price up > 3% AND RSI 30-65 | 2 consecutive red days with declining volume |
| **Gap and Go** | Open > 2.5% above previous close AND volume > 1.5x avg | Price drops below today's open |
| **Momentum Continuation** | Price above SMA20 AND SMA20 slope positive AND RSI 50-70 | Price closes below SMA20 |

**Default Risk:** 6% stop-loss, 8% take-profit, 8% max position size

### 3.3 Mid Cap ($20–$100) — `strategy_mid.py`

**Market Characteristics:** Institutional ownership, follows sectors/indices, momentum works well.

| Strategy | BUY Trigger | EXIT Trigger |
|---|---|---|
| **Sector Momentum** | Stock RSI > 50 AND SPY trending up AND volume > avg | Stock drops below SMA20 OR sector reverses |
| **Breakout with Volume** | Price > 20-day high AND volume > 2x avg AND RSI 55-75 | Price drops below 10-day low |
| **Pullback to Support** | Price pulls back to SMA20 from above AND RSI 40-55 | Price closes below SMA50 |
| **MACD Cross** | MACD crosses above signal AND histogram positive AND price > SMA50 | MACD crosses below signal |

**Default Risk:** 5% stop-loss, 7% take-profit, 8% max position size

### 3.4 Large Cap ($50–$500) — `strategy_large.py`

**Market Characteristics:** Market-correlated, institutional-driven, lower volatility.

| Strategy | BUY Trigger | EXIT Trigger |
|---|---|---|
| **Index Correlation** | SPY RSI < 35 AND stock RSI < 40 | SPY overbought OR stock hits TP |
| **Relative Strength** | Stock outperforms SPY over 5 days AND volume > avg AND RSI < 70 | Stock underperforms SPY 3 consecutive days |
| **Dividend Yield Play** | RSI < 35 AND price > $50 (blue chip proxy) | RSI > 55 |
| **MA Alignment** | Price > EMA12 > SMA20 > SMA50 AND volume > avg | Price closes below SMA20 |

**Default Risk:** 4% stop-loss, 6% take-profit, 7% max position size

### 3.5 Crypto — `strategy_crypto.py`

**Market Characteristics:** 24/7 trading, BTC-correlated, sentiment-driven, trends hard.

| Strategy | BUY Trigger | EXIT Trigger |
|---|---|---|
| **BTC Correlation** | BTC RSI < 45 AND BTC bouncing (positive day) AND alt RSI < 40 | BTC drops below recent low |
| **Trend Following** | Price above SMA20 with momentum (RSI 40-70, positive day) | Price crosses below SMA20 |
| **Extreme Oversold** | RSI < 30 AND price > 10% below SMA20 | RSI > 75 (genuine overbought) |
| **Volume Surge** | Volume > 1.5x avg AND price up > 1.5% AND RSI < 70 | Volume drops below avg 2 periods |

**Default Risk:** 8% stop-loss, 10% take-profit, 7% max position size

### Strategy Scoring System

Each engine runs 4 strategies independently. Votes are combined:

| Vote | Score |
|---|---|
| BUY | +1 |
| SELL | -1 |
| HOLD | 0 |

| Total Score | Signal |
|---|---|
| ≥ 2 | STRONG_BUY |
| 1 | BUY |
| 0 | HOLD |
| -1 | SELL |
| ≤ -2 | STRONG_SELL |

#### Long-only entry invariant (2026-04-15)

The per-size strategies are **long-only entry engines**. They emit `BUY`
or `HOLD` for almost every condition. A `SELL` vote is only returned for
conditions that are *actually bearish setups a short trader would take*
— not "exit conditions for a hypothetical existing long." Examples of
allowed SELL branches: MACD bearish cross, break of 10-day low, failed
gap, falling knife (10 consecutive red closes), SPY RSI > 75.

Exit management for actual open positions (stop loss, take profit,
trailing stop, re-entry cooldown) is a **separate concern** handled by
`trade_pipeline.check_exits` and friends — the entry strategies do
not participate.

Additionally, `multi_strategy.aggregate_candidates()` coerces `SELL`
votes to `HOLD` (with zero score contribution) when the profile has
`enable_short_selling=False`, so long-only profiles cannot be biased
toward `STRONG_SELL` by signal leakage.

**Regression guard:** `tests/test_strategy_sell_bias_fix.py` (18 tests)
pins both invariants — bogus-SELL conditions return HOLD, legit bearish
setups still SELL, aggregation respects the profile flag.

---

## 4. AI-First Analysis Pipeline

### 4.1 Architecture: AI as the Brain

The AI is not a yes/no gate — it is the portfolio manager. One batch call per cycle sees all candidates with full context and picks the best trades.

```
Screen 8000+ symbols → Strategy engines score candidates (free) →
  Rank top ~15 → Single AI batch call with:
    - All candidates + indicators + news headlines
    - Current portfolio state (holdings, P&L, drawdown)
    - Market regime (VIX, SPY trend)
    - Political context (MAGA Mode)
    - Learned patterns from history
    - Per-stock track record
  → AI picks 0-3 trades and sizes them → Execute
```

**Cost:** ~$0.001 per batch call. At 15-min intervals: ~$0.15-0.25/day total.

### 4.2 Multi-Provider Support

| Provider | Models Available | Pricing Tier |
|---|---|---|
| **Anthropic** | Claude Haiku 4.5 (cheapest), Sonnet 4, Opus 4 | ~$0.001/batch call (Haiku) |
| **OpenAI** | GPT-4o-mini, GPT-4o, o3-mini | ~$0.001/batch call (mini) |
| **Google** | Gemini 2.0 Flash, Gemini 2.5 Pro | ~$0.001/batch call (Flash) |

Users select their provider and model per trading profile. The `ai_providers.py` abstraction handles SDK differences and JSON response cleaning.

### 4.3 Batch Prompt Structure

The AI receives a single comprehensive prompt:

```
ROLE: Portfolio manager for automated {market_type} system.
      Pick 0-3 trades from candidates. Zero is valid.

PORTFOLIO STATE:
  - Equity, cash, positions with P&L
  - Drawdown % and status (normal/reduce/pause)

MARKET CONTEXT:
  - Regime (bull/bear/sideways/volatile), VIX, SPY trend
  - Political context (MAGA Mode): sector impact, ticker mentions, trade ideas
  - Track record summary and overall win rate
  - LEARNED PATTERNS from history:
    - "Breakout signals in volatile markets: 15% win rate. Avoid."
    - "Mean reversion midday: 55% win rate. Favor this pattern."

CANDIDATES (ranked by technical score):
  1. AAPL @ $185.50 | STRONG_BUY (score 2/4)
     Votes: breakout=BUY, momentum=BUY
     RSI: 42 | StochRSI: 35 | ADX: 28 | Vol: 2.3x | ROC10: +3.2% | vs 52wH: -8.5%
     Breakout above 20-day high on 2.3x volume
     Your record: 3W/1L (75% win rate)
     News: Apple reports strong iPhone sales | AI chip deal announced
  2. ...

RESPONSE: JSON with trades[], portfolio_reasoning, pass_this_cycle
```

### 4.4 Technical Indicators Fed to AI

**Trend & Momentum (7 indicators):**

| Indicator | Code | What It Measures |
|---|---|---|
| RSI (14) | `rsi` | Overbought/oversold (0-100) |
| Stochastic RSI | `stoch_rsi` | More sensitive RSI variant (0-100) |
| ADX (14) | `adx` | Trend strength (>25 = strong trend) |
| MACD + Signal + Histogram | `macd`, `macd_signal`, `macd_histogram` | Momentum shifts |
| ROC (10) | `roc_10` | Rate of Change — momentum as % |
| SMA 20, 50 | `sma_20`, `sma_50` | Moving average trend direction |
| EMA 12 | `ema_12` | Short-term trend sensitivity |

**Institutional Money Flow (4 indicators):**

| Indicator | Code | What It Measures |
|---|---|---|
| MFI (14) | `mfi` | Money Flow Index — volume-weighted RSI. Shows if institutions are buying (>50) or selling (<50). |
| CMF (20) | `cmf` | Chaikin Money Flow — positive = accumulation, negative = distribution |
| OBV | `obv` | On-Balance Volume — running total showing if volume flows with or against price |
| A/D Line | `ad_line` | Accumulation/Distribution — confirms trend with volume |

**Volatility & Structure (5 indicators):**

| Indicator | Code | What It Measures |
|---|---|---|
| ATR (14) | `atr_14` | Average True Range — stock's actual volatility in dollars |
| Bollinger Bands | `bb_upper`, `bb_lower`, `bb_middle` | Volatility channels (2σ from SMA20) |
| Volatility Squeeze | `squeeze` | 1 when Bollinger Bands are inside Keltner Channels — big move imminent |
| VWAP (20) | `vwap_20`, `pct_from_vwap` | Volume-Weighted Average Price — institutional benchmark |
| Volume vs SMA | `volume_sma_20` | Volume anomaly detection |

**Price Context (5 indicators):**

| Indicator | Code | What It Measures |
|---|---|---|
| 52-Week High/Low | `pct_from_52w_high`, `pct_from_52w_low` | Where price sits in yearly range |
| Fibonacci Levels | `fib_382`, `fib_500`, `fib_618`, `nearest_fib_dist` | Key retracement levels where institutional orders cluster |
| Pivot Points | `pivot`, `resistance_1`, `support_1` | Previous-day derived support/resistance |
| Gap % | `gap_pct` | Opening gap from previous close — unfilled gaps act as price magnets |

**Sector Context (per candidate):**

| Data | Source | What It Provides |
|---|---|---|
| Sector Rotation | 11 sector ETFs (XLK, XLF, XLE, etc.) | Which sectors have inflows/outflows this week |
| Relative Strength | Stock 5d return vs sector ETF 5d return | Is this stock leading or lagging its sector? |

**Per-Stock News (up to 3 headlines per candidate):**
Free from yfinance, cached 30 min. AI sees actual news catalysts alongside technicals.

All computed from the `ta` library on free yfinance data. **33 technical indicators + alternative data (insider, short interest, options flow, fundamentals, intraday) + sector context + news = zero API cost.**

### 4.5 AI Response Schema

```json
{
  "trades": [
    {
      "symbol": "AAPL",
      "action": "BUY",
      "size_pct": 7.5,
      "confidence": 75,
      "stop_loss_pct": 3.0,
      "take_profit_pct": 10.0,
      "reasoning": "Strong breakout with volume confirmation..."
    }
  ],
  "portfolio_reasoning": "Why this combination or why pass",
  "pass_this_cycle": false
}
```

Validation: symbols must be in candidates list, size clamped to max_position_pct, max 3 trades, shorts only if enabled. On failure: 0 trades (safe default).

### 4.6 JSON Response Handling

All AI responses go through `_strip_markdown_fences()` which:
1. Removes ` ```json ``` ` wrappers
2. Extracts the first complete `{...}` JSON object using brace matching
3. Handles preamble text and trailing commentary
4. Works across all providers (Haiku, GPT, Gemini)

---

## 5. Trade Execution Pipeline

### 5.1 Pipeline Steps

```
Step 0: PORTFOLIO STATE (fetched ONCE per cycle)
  ├─ Account info (equity, cash, buying power)
  ├─ Current positions (filtered by market type)
  ├─ Drawdown check (pause at 20%, reduce at 10%)
  └─ If drawdown pause → return immediately, zero AI calls

Step 1: PRE-FILTER (zero AI cost)
  ├─ Auto-blacklisted symbols (0% win rate on 3+ predictions)
  ├─ Earnings within avoid_days
  ├─ At max positions and symbol not held
  └─ Excluded symbols (user's restricted list)

Step 2: STRATEGY (CPU cost, no AI cost)
  ├─ Route to market-specific engine via strategy_router.py
  ├─ Run 4 strategies per symbol, combine votes into score
  └─ All candidates scored in parallel (no AI calls)

Step 3: RANK & SHORTLIST (zero cost)
  ├─ Filter: HOLD → drop, SELL with no position + no shorts → drop
  ├─ Sort by abs(score) descending
  └─ Take top 15 candidates

Step 4: AI BATCH SELECTION (ONE AI call)
  ├─ Lazy-fetch MAGA political context (only when shortlist non-empty)
  ├─ Build batch prompt: candidates + portfolio + regime + patterns + news
  ├─ Single call to ai_select_trades()
  └─ AI returns 0-3 trades with sizing and reasoning

Step 5: EXECUTE (per AI-selected trade)
  ├─ ATR-based stop/take-profit calculation
  ├─ Correlation check (reduce size if > 0.7)
  ├─ Position sizing from AI's size_pct
  ├─ Submit order via Alpaca API (market or limit)
  └─ Log trade to journal
```

### 5.2 Pipeline Efficiency

Logged every cycle: `"27 candidates → 23 post-filter → 1 shortlisted → 1 AI call → 0 buys"`

Typical cycle cost: **1-2 AI calls** (batch + MAGA if needed). ~$0.001-0.002 per cycle.

### 5.3 Dynamic Universe Discovery

Instead of hardcoded symbol lists, the system dynamically discovers tradable symbols:

1. Alpaca API `list_assets()` returns ~8000+ tradable US equities
2. Random sample of 500 + full hardcoded fallback universe
3. yfinance batch download filters by price range and volume
4. Top 100 most active symbols returned
5. Cached for 24 hours

Hardcoded lists in segments.py serve as the fallback if dynamic discovery fails. Crypto uses a fixed universe (33 pairs) since crypto symbols are well-known.

### 5.4 Per-Stock News Integration

Each shortlisted candidate gets up to 3 recent headlines from yfinance (free, cached 30 min). Headlines are included in the AI batch prompt so the AI can factor in news catalysts without a separate AI call.

---

## 6. Risk Management

### 6.1 Position-Level Controls

| Control | How It Works |
|---|---|
| **ATR-Based Stops** | Stop-loss = entry - (2× ATR), take-profit = entry + (3× ATR). Adapts to each stock's volatility. |
| **Trailing Stops** | Once profitable, stop follows price up (longs) or down (shorts). Trail distance = 1.5× ATR. Never turns a winner into a big loser. |
| **Fixed % Stops** | Fallback when ATR data unavailable. Configurable per profile. |
| **Short-Specific Stops** | Separate wider stops for shorts (default 8%) because upward volatility spikes are sharper. |
| **Limit Orders** | Optional (default off). Entries use limit orders at current price for better fills. Unfilled orders auto-cancelled after 5 minutes. Exit orders remain market for guaranteed fill. |

### 6.2 Portfolio-Level Controls

| Control | Default | How It Works |
|---|---|---|
| **Max Position Size** | 8-10% of equity | No single position exceeds this % |
| **Max Total Positions** | 10 | Won't open new positions beyond this count |
| **Drawdown Reduction** | 10% drawdown | Halves all position sizes |
| **Drawdown Pause** | 20% drawdown | Stops all trading until recovery |
| **Correlation Limit** | 0.7 | Reduces position size 50% when new trade is > 70% correlated with existing positions |
| **Sector Limit** | 5 per group | Limits exposure to correlated market segments |

### 6.3 Pre-Trade Filters

| Filter | How It Works |
|---|---|
| **Earnings Avoidance** | Skips stocks within N days of earnings (default 2) |
| **Auto-Blacklist** | Skips stocks with 0% win rate on 3+ predictions |
| **Restricted Symbols** | User-defined exclusion list (e.g., employer stock) |
| **Bounce-Day Short Filter** | Only opens shorts when stock is UP intraday (not shorting into weakness) |

---

## 7. Self-Tuning & Machine Learning

### 7.0 Current State — 12-Layer Autonomy (v5.0, April 2026)

The original 4-parameter tuner described in §7.2 below has expanded
into a 9-layer architecture (plus cost guard) covering ~50 distinct
autonomous decision surfaces. Canonical reference is
`AUTONOMOUS_TUNING_PLAN.md`; per-rule detail is in `SELF_TUNING.md`.

| Layer | Surface | Module |
|---|---|---|
| 1 | 35+ tunable parameters with cooldown / reverse / bound clamping | `self_tuning.py` + `param_bounds.py` |
| 2 | Per-signal weighted intensity (4-step ladder: 1.0/0.7/0.4/0.0) | `signal_weights.py` |
| 3 | Per-regime parameter overrides (bull/bear/sideways/volatile/crisis) | `regime_overrides.py` |
| 4 | Per-time-of-day overrides (open/midday/close ET) | `tod_overrides.py` |
| 5 | Cross-profile insight propagation | `insight_propagation.py` |
| 6 | Adaptive AI prompt structure (cost-gated) | `prompt_layout.py` |
| 7 | Per-symbol parameter overrides (most-specific tier) | `symbol_overrides.py` |
| 8 | Self-commissioned new strategies (cost-gated) | `self_tuning._optimize_commission_strategy` + `strategy_proposer.py` |
| 9 | Auto capital allocation (opt-in, per-Alpaca-account-conserving) | `capital_allocator.py` |
| ✱ | Cost guard (cross-cutting daily-spend ceiling) | `cost_guard.py` |

**Closed-loop learning surfaces** (operate alongside the tuning layers):
- **Meta-model** (`meta_model.py`): gradient-boosted classifier trained
  daily per profile; re-weights AI confidence at decision time.
- **Alpha decay monitor** (`alpha_decay.py`): tracks rolling 30-day
  Sharpe per strategy; auto-deprecates when degraded for 30+ days,
  auto-restores when recovered for 14+ days.
- **Losing-week post-mortems** (`post_mortem.py`): weekly Sunday task;
  when the past 7 days underperformed baseline by ≥10pt, clusters
  losing predictions by feature signature and stores the dominant
  pattern as a `learned_pattern` that the AI prompt picks up.
- **False-negative analysis** (`self_tuning._optimize_false_negatives`):
  HOLD predictions that resolve as "loss" (price moved enough to be
  a missed opportunity) trigger threshold loosening when clustered.

**Decision-time parameter resolution** uses a precedence chain at
every read:
```
per-symbol > per-regime > per-time-of-day > profile-global > caller-default
× capital_scale (Layer 9 multiplier, opt-in)
```
Single entry point: `regime_overrides.resolve_for_current_regime(
profile, name, default=..., symbol=...)`. Wired into `trade_pipeline`
at every parameter read.

**Anti-regression guardrails** (six structural tests):
1. `test_no_recommendation_only` — every "Recommendation:" string in
   `self_tuning.py` must be on an explicit allowlist with rationale.
2. `test_no_snake_case_in_optimizer_strings` — optimizer return
   strings can't embed raw column names.
3. `test_no_snake_case_in_api_responses` — dynamically discovers every
   `/api/*` endpoint, walks JSON responses, fails on raw PARAM_BOUNDS
   keys in user-facing positions.
4. `test_no_duplicate_dom_ids` — every `id="..."` in templates must be
   unique within its file (prevents JS getElementById from silently
   orphaning widgets).
5. `test_self_tune_task_no_change_path` — the no-change branch can't
   NameError.
6. `test_every_lever_is_tuned` — every column in `trading_profiles` is
   either auto-tuned or on the `MANUAL_PARAMETERS` allowlist with a
   written rationale.

The legacy detail in §7.2–§7.10 below is retained as historical
reference for the disaster-prevention + upward-optimization modes that
predate the layered architecture; both still operate as Layer 1 rules.

### 7.1 Performance Context Injection

Before every AI review, the prompt includes the AI's own track record:

```
MARKET: BEAR (VIX 25)
YOUR RECORD ON SOFI: 2W/6L (25% win rate)
YOUR OVERALL: 45% win rate (94W/112L). Be more selective.
EARNINGS: SOFI reports in 3 days. High uncertainty.
```

This forces the AI to consider its own accuracy when making decisions.

### 7.2 Automatic Parameter Adjustment

**Status (2026-04-22):** All 10 active profiles have sufficient resolved predictions and are actively tuning daily at EOD. The self-tuner operates in two modes:

- **Disaster prevention** (win rate < 35%): raises confidence threshold, reduces position size, widens short stops
- **Upward optimization** (win rate >= 35%): actively seeks higher win rates via confidence band analysis, regime-aware sizing, strategy selection, stop/TP tuning, and position size increases

See `SELF_TUNING.md` for complete documentation of all 5 upward optimization strategies.

Runs daily at 3:55 PM ET:

1. **Review past adjustments** (3+ days old with 10+ new predictions)
   - If adjustment improved win rate → mark "improved," keep it
   - If adjustment worsened win rate → auto-reverse it
   - 3-day cooldown prevents oscillation

2. **Make new adjustments** based on current data:
   - If win rate at confidence < 60% is below 35% → raise threshold to 60
   - If win rate at confidence < 70% is below 35% → raise to 70
   - If overall win rate < 30% → reduce position size by 20%
   - If short selling has 0% win rate on 5+ trades → widen short stops 50%

3. **Upward optimization** (when win rate >= 35%):
   - Find the best confidence band and raise threshold to focus on it
   - Reduce position size in losing market regimes, increase in winning ones
   - Disable worst-performing strategies (never the last one)
   - Widen stops that trigger too early, tighten take-profits that never hit
   - Increase position size when edge is proven (55%+ WR, 30+ samples)
   - One change per run for clean auto-reversal attribution

### 7.3 Tuning Memory

Every adjustment is logged with full context in `tuning_history`:

```
| Date       | Parameter              | Old → New  | Win Rate Then | Outcome  |
|------------|------------------------|------------|---------------|----------|
| 2026-04-02 | ai_confidence_threshold| 25 → 70    | 8.8%          | Improved |
| 2026-04-02 | max_position_pct       | 0.10 → 0.08| 8.8%          | Improved |
```

Future adjustments check this history to avoid repeating strategies that already failed.

### 7.4 Pattern Learning

The system analyzes historical predictions to discover failure/success patterns beyond simple win/loss counts. `_analyze_failure_patterns()` queries `ai_predictions` grouped by:

- **Market regime** at prediction time (stored in `regime_at_prediction` column)
- **Strategy type** that generated the signal (stored in `strategy_type` column)
- **Time of day** when the prediction was made

Example patterns surfaced to the AI:

```
LEARNED PATTERNS (from your history):
- Predictions in volatile markets: 15% win rate (vs 45% overall). Be extra cautious.
- breakout signals: 22% win rate (60 trades). Avoid this pattern.
- Predictions at 9:00-10:00: 20% win rate (25 trades). Avoid trading this hour.
- mean_reversion signals: 65% win rate (30 trades). Favor this pattern.
```

These patterns are included in every AI batch prompt so the AI can apply conditional reasoning, not just aggregate statistics.

### 7.5 Meta-Model (Phase 1 of Quant Fund Evolution)

A second-layer gradient-boosted classifier trained on the system's own prediction history. It learns **when the AI is likely to be wrong** and re-weights confidence before execution. See `meta_model.py` and `ROADMAP.md`.

**The Insight:** The AI is a generalist with systematic blind spots. Our resolved prediction database captures those blind spots in labeled form. A classifier learns patterns like "AI overconfident on low-volume mid-caps in sideways markets with RSI 45-55." The training data is our proprietary AI predictions — literally impossible for competitors to replicate.

**Data Flow:**

```
1. AI makes prediction -> full feature context stored in ai_predictions.features_json
2. Prediction resolves (win/loss) via existing resolution job
3. Daily at snapshot time: retrain meta-model on resolved predictions (>=100 samples)
4. Live: before execution, meta-model estimates P(AI correct) for each selected trade
5. Execution rules:
   - meta_prob >= 0.3: blend confidence = ai_conf * (0.5 + meta_prob * 0.5)
   - meta_prob < 0.3: suppress trade entirely
```

**Features the meta-model uses:**

- Numeric: all 33 technical indicators (RSI, ADX, MFI, CMF, etc.), relative strength vs sector, short interest, PE ratio, Reddit mentions/sentiment, market signal count
- Categorical (one-hot encoded): signal direction, insider direction, options signal, VWAP position, sector trend, market regime

**Model Architecture:** `sklearn.ensemble.GradientBoostingClassifier` — 100 estimators, depth 3, learning rate 0.05. Chosen over XGBoost for simplicity and no extra dependencies. Trained with 80/20 train/test split, stratified.

**Metrics Tracked:**

- AUC (area under ROC curve): how well the model separates winners from losers. Target >= 0.55 (better than random).
- Accuracy: overall hit rate on held-out test set
- Positive rate: baseline win rate for comparison
- Feature importance: which inputs most strongly predict AI correctness

**Retraining:** Daily at end-of-day snapshot time. Model saved to `meta_model_{profile_id}.pkl`. Requires >=100 resolved predictions with `features_json` populated to train.

**Dashboard:** Performance page AI Intelligence tab shows per-profile: current AUC, accuracy, training sample count, top 10 most predictive features. When insufficient data is available, shows "Collecting training data" placeholder.

**Why This Matters:** Most systems use AI as a single decision layer. This creates a second layer that ML-learns from the first layer's mistakes. Each new prediction strengthens the meta-model. Over time the system develops a sophisticated model of its own error patterns — compounding alpha that competitors cannot replicate because it's trained on our proprietary prediction data.

### 7.6 Rigorous Validation Gate (Phase 2 of Quant Fund Evolution)

The discipline that 90% of quant funds skip. No strategy goes live without passing this gauntlet. See `rigorous_backtest.py` and `ROADMAP.md`.

**Entry Point:**

```python
from rigorous_backtest import validate_strategy, save_validation

result = validate_strategy(
    strategy_fn=my_strategy,
    market_type='midcap',
    history_days=540,        # ~2 years
    params=None,
)

if result['verdict'] == 'PASS':
    save_validation('my_strategy', result)
    # now safe to deploy
else:
    print(result['failed_gates'])
```

**The 10 Gates (all must PASS):**

| # | Gate | What It Checks | Threshold |
|---|---|---|---|
| 1 | Min Trades | Enough trades for statistical meaning | ≥ 30 |
| 2 | Sharpe Ratio | Risk-adjusted return | ≥ 1.0 |
| 3 | Max Drawdown | Worst peak-to-trough | ≥ -25% |
| 4 | Win Rate | Trade hit rate | ≥ 35% |
| 5 | Statistical Significance | t-test on Sharpe ratio | p < 0.05 |
| 6 | Monte Carlo | Bootstrap resample 1000× | ≥ 60% positive |
| 7 | Out-of-Sample | Held-out 20% not overfit | OOS Sharpe drop ≤ 30% |
| 8 | Regime Consistency | Works across market conditions | ≥ 2 regimes profitable |
| 9 | Walk-Forward | Stability across time windows | ≥ 50% folds profitable |
| 10 | Capacity | Position size vs daily volume | ≤ 1% of daily volume |

**Gate Details:**

- **Statistical Significance**: Computes t-statistic on per-trade returns against H0: Sharpe = 0. Uses scipy for proper t-distribution when available, normal approximation otherwise.
- **Monte Carlo Stress Test**: Bootstrap-resamples trade outcomes 1000 times, subtracts realistic transaction costs (0.2% entry + 0.2% exit), reports percentile statistics. A strategy passes only if >60% of resampled scenarios are profitable.
- **Out-of-Sample Degradation**: Runs backtest on in-sample window, then on held-out OOS window. Fails if OOS Sharpe drops more than 30% (classic overfit signature).
- **Regime Consistency**: Partitions trades by market regime (bull/bear/sideways/volatile). A strategy that only works in one regime is curve-fit and fails this gate.
- **Walk-Forward Analysis**: Splits history into sequential non-overlapping folds. Strategy must be profitable in >50% of folds — confirms edge is stable over time.
- **Capacity Analysis**: For each trade, computes position value / average daily dollar volume. Positions above 1% of daily volume will experience significant slippage at scale. Also projects USD capacity ceiling.
- **Transaction Cost Modeling**: Every Monte Carlo iteration subtracts 0.4% round-trip cost (configurable). This prevents strategies that only look good before slippage.

**Persistence:** All validation runs saved to `strategy_validations.db` with full gate-by-gate results, metrics, and configuration. Powers the Phase 3 alpha decay monitoring layer.

**Dashboard:** Performance page AI Intelligence tab shows all recent validations with verdict, score, gate pass/fail counts, and elapsed time.

**Default Thresholds** (in `rigorous_backtest.THRESHOLDS`):

```python
THRESHOLDS = {
    "min_total_trades": 30,
    "min_sharpe": 1.0,
    "min_sortino": 1.0,
    "max_drawdown_pct": -25.0,
    "min_win_rate": 35.0,
    "min_profit_factor": 1.3,
    "max_p_value": 0.05,
    "max_oos_sharpe_degradation_pct": 30.0,
    "min_regimes_profitable": 2,
    "min_monte_carlo_positive_pct": 60.0,
    "max_pct_daily_volume": 0.01,
}
```

These thresholds are the absolute minimum bar. Strategies exceeding them meaningfully (Sharpe > 1.5, OOS degradation < 10%, etc.) are ranked higher for deployment priority.

**Why This Matters:** Every institutional failure story — LTCM, Archegos, Melvin — traces back to insufficient rigor before deployment. The largest funds in the world deploy strategies that haven't been walked forward, stress tested, or validated out-of-sample. We won't. This gate is non-negotiable and applies to every phase that follows (auto-generated strategies in Phase 7 must pass before promotion to live; multi-strategy capital allocation in Phase 6 only considers strategies that have passed validation).

**Performance (Phase 2.1 optimization):** A full 5-strategy validation originally took ~25 minutes. With per-symbol yfinance caching and one-time indicator precomputation (strategies reuse the prepopulated DataFrame instead of recomputing all 33 indicators on every day's window), the same run completes in ~4 minutes — a 5.97x speedup. This makes Phase 7 (auto-generation) practical because it requires validating dozens of proposed strategy variants per cycle.

### 7.7 Alpha Decay Monitoring (Phase 3 of Quant Fund Evolution)

Every signal decays. Momentum worked for decades then got arbitraged away. Value investing stopped paying in the 2010s. Most retail AND institutional systems cling to dead strategies because nobody rigorously measures decay. This module fixes that.

See `alpha_decay.py` and `ROADMAP.md`.

**Data Flow:**

```
Every resolved prediction → ai_predictions (existing)
Daily task: snapshot_all_strategies() → signal_performance_history
         → detect_decay() per strategy_type
         → deprecate_strategy() if 30-day rolling Sharpe stays
           >=30% below lifetime for 30+ consecutive days
         → restore_strategy() if rolling edge recovers
Trade pipeline: _rank_candidates() skips deprecated strategies' signals
```

**Detection Algorithm:**

1. Compute lifetime Sharpe from all resolved predictions (`strategy_type` column on `ai_predictions`)
2. Write a daily snapshot of 30-day rolling metrics per strategy to `signal_performance_history`
3. Check if rolling Sharpe < lifetime × (1 - 30%) for 30 consecutive snapshot days
4. If yes → insert row into `deprecated_strategies`, pipeline stops using that strategy's signals
5. If a deprecated strategy's rolling Sharpe recovers to within 15% of lifetime for 14 consecutive days → restore it

**Tables (per-profile journal DB):**

| Table | Purpose |
|---|---|
| `signal_performance_history` | Daily snapshot rows: date, strategy_type, window_days, n_predictions, wins, losses, win_rate, avg_return_pct, sharpe_ratio, profit_factor |
| `deprecated_strategies` | Current deprecation state: strategy_type PRIMARY KEY, deprecated_at, reason, rolling_sharpe_at_deprecation, lifetime_sharpe, consecutive_bad_days, restored_at |

**Thresholds** (in `alpha_decay.DECAY_THRESHOLDS`):

```python
DECAY_THRESHOLDS = {
    "rolling_window_days": 30,
    "lifetime_min_predictions": 50,
    "rolling_min_predictions": 10,
    "sharpe_degradation_pct": 30.0,
    "consecutive_bad_days": 30,
    "restoration_recovery_pct": 15.0,
    "restoration_good_days": 14,
}
```

**Scheduled Task:** `_task_alpha_decay(ctx)` in `multi_scheduler.py` runs daily at snapshot time alongside self-tuning and meta-model retraining.

**Dashboard:** Performance page AI Intelligence tab shows per-profile rolling vs lifetime Sharpe for each strategy, edge change %, and any currently-deprecated strategies with the reason they were retired.

**Why This Matters:** Without automatic decay monitoring, a strategy that was profitable for years can silently stop working and drain equity for months before anyone notices. This module catches it within weeks of actual decay and removes the strategy from the candidate pool automatically. Combined with Phase 7 (auto-generation), the strategy library refreshes continuously: dying strategies retire, new variants get proposed, validated, and deployed. The alpha pool stays fresh.

### 7.8 SEC Filings Semantic Analysis (Phase 4 of Quant Fund Evolution)

10-K (annual), 10-Q (quarterly), and 8-K (current report) filings are public and free on SEC EDGAR. They contain some of the strongest predictive signals in finance — new risk factor language, going concern disclosures, material weakness admissions, MD&A forward-looking tone shifts — but nobody reads 200-page documents by hand. LLMs can read them instantly and diff consecutive filings to surface material changes.

See `sec_filings.py` and `ROADMAP.md`.

**Data Flow:**

```
Daily task: for each held/shortlist equity symbol:
  1. Resolve ticker → CIK via SEC's free company_tickers.json mapping
  2. List recent filings (10-K/10-Q/8-K) from EDGAR submissions JSON
  3. Skip any already in sec_filings_history
  4. For each new filing:
       a. Fetch filing HTML via rate-limited EDGAR request
       b. Extract Risk Factors and MD&A sections with regex anchors
       c. Flag "going concern" and "material weakness in internal control"
       d. AI diff risk factors against previous filing of same type
       e. Persist row with alert severity/signal/summary
Trade pipeline: _build_candidates_data() reads active alerts (filed <= 90
days ago) for shortlist symbols and injects them into the AI prompt.
```

**Tables (per-profile journal DB):**

| Column | Purpose |
|---|---|
| symbol, accession_number, form_type, filed_date | Filing identity |
| filing_url, fetched_at | Source URL and when we pulled it |
| risk_factors_text, mdna_text | Extracted section bodies |
| going_concern_flag, material_weakness_flag | Boolean red-flag indicators |
| analyzed_at, alert_severity, alert_signal, alert_summary, alert_changes_json | AI diff results |

**AI Diff Output Schema:**

```json
{
  "severity": "low | medium | high",
  "signal": "concerning | positive | neutral",
  "summary": "one-sentence human-readable",
  "changes": [
    {"type": "new_risk | removed_language | language_shift",
     "old": "...", "new": "...", "impact": "trade short | avoid | none"}
  ]
}
```

**Integration with AI batch prompt:** Medium- and high-severity alerts appear on the candidate line in the form:

```
SEC ALERT [HIGH/concerning]: 10-Q filed 2024-04-01 — New going concern
language added to risk factors following covenant breach.
```

The AI can now condition its trade decision on breaking corporate disclosures without any additional data ingestion work.

**Rate limits:** SEC asks for <10 req/sec and a contactable User-Agent. The module sleeps between requests and identifies itself as `QuantOpsAI Research Bot (mack@mackenziesmith.com)`. All filing bodies cache for 24 hours (filings are immutable, but cache prevents repeat fetches during a single run).

**Crypto profiles are skipped** — SEC filings don't apply.

**Scheduled Task:** `_task_sec_filings(ctx)` in `multi_scheduler.py` runs daily at snapshot time. Processes held positions plus symbols from the most recent cycle's shortlist.

**Dashboard:** Performance page AI Intelligence tab includes a "SEC Filing Alerts" panel showing all active (≤90 day) alerts with severity, signal, and summary.

**Why This Matters:** When a CEO inserts one new paragraph into a 10-K risk factors section about "material uncertainty regarding continued operations," the stock typically drops 10-40% over the next quarter. Humans miss this because the filing is 200 pages; institutional analysts catch it but only for their watchlist of a few dozen names. Our system scans every held position plus every shortlist candidate daily. This is genuine alternative data at our scale almost no one has.

### 7.9 Options Chain Oracle (Phase 5 of Quant Fund Evolution)

Options markets are the forward-expectation layer on top of the spot market. Institutional traders pay real money for optionality — the prices they pay reveal what they actually believe will happen. Most retail systems see only "did a call print green today" and call it "options flow." We extract the real signals.

See `options_oracle.py` and `ROADMAP.md`.

**Seven institutional-grade signals, all from free yfinance chains:**

| Signal | What It Reveals |
|---|---|
| **IV Skew** | Put IV vs call IV asymmetry. Skew > 1.3 = market fear; < 0.85 = greed. Contrarian signals at extremes. |
| **IV Term Structure** | IV across expirations. Normal = upward slope; inverted = imminent event (earnings, catalysts) expected. |
| **Implied Move** | Market-implied 1σ move from ATM straddle price. A 5% move in 4 days = major catalyst priced in. |
| **Put/Call Ratios** | Volume PCR (intraday flow) and OI PCR (positioning). > 1.2 = bearish, < 0.5 = bullish. |
| **Gamma Exposure (GEX)** | Dealer hedging regime. Positive = pinning / vol contraction; negative = vol expansion. |
| **Max Pain** | Strike where option holders collectively lose the most. Price gravitates here near expiration. |
| **IV Rank** | Current IV percentile vs 52-week realized vol proxy. High = sell premium, low = buy premium. |

**Flow:**

```
Shortlist candidate → get_options_oracle(symbol) →
  fetch nearest 3 expirations' chains (cached 30 min) →
  compute all 7 signals →
  summarize_for_ai() → compact one-line summary →
  injected into AI batch prompt as "OPTIONS: ..."
```

**Example prompt injection:**

```
OPTIONS: skew=fear(1.42) | IV TERM INVERTED | implied_move=6.2%/4d | PCR=1.85(bearish_flow) | gex=volatility_expansion | iv_rank=iv_high
```

The AI reads this and knows: institutional traders are pricing in a big downward move within 4 days, options are expensive, and dealers are short gamma (so volatility will amplify). That's enough context to size a short or avoid a long position entirely — institutional intelligence impossible to derive from price alone.

**Crypto is skipped** — yfinance doesn't have crypto options chains at retail scale.

**Caching:** 30-minute TTL per symbol. Options data changes during the session but not every cycle. This matches our 15-min scan cadence well.

**Why This Matters:** Implied volatility, gamma positioning, and skew are the three signals institutional options desks watch every hour. Retail traders don't even know these exist. Hedge funds pay $2,000+/month per seat for Bloomberg or Tradytics to see them. We compute them in milliseconds from a free yfinance chain. Every time the AI evaluates a candidate, it sees what the smartest money in the world thinks about that stock's future.

### 7.10 Multi-Strategy Capital Allocation (Phase 6 of Quant Fund Evolution)

Real quant funds don't run one strategy — they run dozens of uncorrelated strategies in parallel and split capital across them by risk contribution. Each strategy has a small edge; combined, they dominate single-strategy systems. See `strategies/`, `multi_strategy.py`, and `ROADMAP.md`.

**Strategy registry (`strategies/__init__.py`):** Every alpha strategy is a self-contained module exposing a uniform interface:

```python
NAME = "my_strategy"                          # must match strategy_type in ai_predictions
APPLICABLE_MARKETS = ["small", "midcap"]      # or ["*"] for every market
def find_candidates(ctx, universe) -> list[dict]: ...
```

The registry discovers modules, filters by market type, and skips any strategy present in the per-profile `deprecated_strategies` table (set by Phase 3 alpha decay monitoring).

**Current strategies (16):**

Core 6 (Phase 6 seed):

| Strategy | Markets | Trigger |
|---|---|---|
| Market Structure Engine | All | Per-market router (momentum/breakout/mean-reversion/gap) preserved as one voter |
| Insider Buying Cluster | Equities | 3+ insider buys totaling ≥ $250K dominating sells |
| Earnings Drift | Equities | Post-announcement move > 5% in line with beat/miss direction |
| Volatility Regime | Equities | Options GEX in volatility-expansion regime (dealer short gamma) |
| Max Pain Pinning | Equities | Price trading away from max pain within 5 days of expiration |
| Gap Reversal | Equities | >3% opening gap on normal-or-lower volume, no catalyst |

Expanded seed library (10 additional, added 2026-04-14):

| Strategy | Markets | Trigger |
|---|---|---|
| Short-Term Reversal | Micro/Small/Midcap | 3-day decline + RSI < 35 + pullback ≥ 3% from 5d high (Jegadeesh/Lehmann) |
| Sector Momentum Rotation | Small/Midcap/Largecap | Symbol belongs to a top-2 or bottom-2 sector by 5d return (Moskowitz/Asness) |
| Analyst Revision Drift | Small/Midcap/Largecap | Fresh upgrade/downgrade within 5 days + price confirming (Womack) |
| 52-Week Breakout | Small/Midcap/Largecap | New 52-week high on ≥ 1.5× avg volume, capped at +15% daily (George/Hwang) |
| Short Squeeze Setup | Micro/Small/Midcap | Short interest > 15% + breakout above 20d high on volume surge |
| High IV Rank Fade | Midcap/Largecap | IV rank > 80 + RSI extreme — fade the move as premium-sellers hedge |
| Insider Selling Cluster | Equities | 3+ insider sells totaling ≥ $500K dominating buys (Seyhun; bearish) |
| News Sentiment Spike | All | Directional sentiment score ≥ 70 + price confirming ≥ 1% (Tetlock/Garcia) |
| Volume Dry-up Breakout | Small/Midcap/Largecap | 5d declining volume consolidation, then break above 10d high on 2× avg |
| MACD Cross Confirmation | All | MACD zero-line cross + RSI in trending zone + 1.2× volume confirmation |

Each row is a module in `strategies/` with a uniform interface. Markets column shows where the strategy is applicable; `get_active_strategies(market_type)` returns the subset that applies.

**Aggregation (`multi_strategy.aggregate_candidates`):** Every active strategy runs across the universe. Each proposes candidates; duplicates are merged by symbol with votes recorded per strategy. Score updates on agreement (bump) or conflict (dampen). Final signal re-derived: score ≥ 2 → STRONG_BUY, 1 → BUY, -1 → SELL, ≤ -2 → STRONG_SELL. One strategy failing does NOT abort the pipeline.

**Capital allocation (`compute_capital_allocations`):** Inverse-variance (risk-parity) weighting based on each strategy's 30-day rolling Sharpe.

```
New strategy (<20 resolved predictions)  → DEFAULT_WEIGHT = 1/6 baseline
Losing strategy (Sharpe ≤ 0)             → DEFAULT_WEIGHT × 0.25 (minimum)
Proven strategy (Sharpe > 0)             → min(Sharpe, 4.0) raw weight
```

Raw weights are normalized to sum to 1.0. **No single strategy may exceed 40% of capital** — excess is redistributed proportionally to strategies under the cap, iterated until stable. If only one strategy is active, it keeps 100% (nowhere to redistribute).

**Pipeline integration (`trade_pipeline.py` Step 3):** The former `strategy_router.run_strategy(symbol, market_type)` single-call is replaced by `aggregate_candidates(ctx, filtered_candidates, db_path=ctx.db_path)`. Every downstream stage — AI batch, risk gates, execution — sees the merged multi-strategy view.

**Dashboard panel:** `Strategy Allocation` on `/ai` shows per-profile per-strategy weight, rolling Sharpe, lifetime Sharpe, resolved prediction count, and win rate. New strategies display as "default (insufficient history)" until they accumulate track record.

**Why This Matters:** Every additional uncorrelated strategy adds marginal alpha that compounds with the others. Institutional funds gate capital by risk contribution, not by "which strategy feels good" — that discipline is the single biggest separator between hobbyist systems and real quant funds. Adding a new strategy is now a two-line change: drop a module into `strategies/`, list it in `STRATEGY_MODULES`, and the registry, validation gate, decay monitor, and capital allocator handle the rest automatically.

### 7.11 Evolving Strategy Library (Phase 7 of Quant Fund Evolution)

Most quant systems have a fixed library that ages out; our library evolves. The AI proposes new strategy *specs* (pure JSON, never Python), each validated against an allowlisted grammar, rendered into a deterministic module, backtested, and — if it earns it — promoted to shadow and then live trading. Failures retire automatically. See `strategy_generator.py`, `strategy_proposer.py`, `strategy_lifecycle.py`, and `ROADMAP.md`.

**Spec grammar.** The AI never writes Python. It writes JSON matching this schema:

```json
{
  "name": "auto_oversold_vol",
  "description": "Deep oversold with volume confirmation",
  "applicable_markets": ["small", "midcap"],
  "direction": "BUY",
  "score": 2,
  "conditions": [
    {"field": "rsi", "op": "<", "value": 25},
    {"field": "volume_ratio", "op": ">", "value": 1.8},
    {"field": "close", "op": ">", "field_ref": "sma_50"}
  ]
}
```

Every `field`, `op`, `direction`, and `applicable_markets` value is checked against a closed allowlist before the spec is accepted. An AI-proposed spec cannot smuggle in arbitrary code, data, or side effects — the generator only knows how to read the permitted fields. Allowed fields include the 20+ indicator columns produced by `add_indicators` plus seven derived fields (`volume_ratio`, `gap_pct`, `range_position`, etc.) that `evaluate_conditions` computes from bars on demand.

**Code generation.** `render_strategy_module(spec)` fills a fixed Python template with `repr()`-escaped values. The resulting file is deterministic and safe to exec. Every auto-strategy module declares `AUTO_GENERATED = True`; the registry uses that flag plus the lifecycle status to decide which ones drive real trades versus which ones are merely recording predictions.

**Lifecycle.** Each auto-strategy flows through five states:

| Status | Meaning |
|---|---|
| `proposed` | AI wrote the spec; awaiting backtest |
| `validated` | Passed Phase 2 `validate_strategy()` — walk-forward, OOS, Monte Carlo, statistical significance |
| `shadow` | Running live but `get_active_strategies()` excludes it from the trade pipeline. `aggregate_shadow_candidates()` runs it so its predictions are recorded and measurable. |
| `active` | Promoted after ≥50 resolved shadow predictions with rolling Sharpe ≥ 0.8 and no decay trigger. Now drives real capital. |
| `retired` | Failed validation, hit shadow period without edge (60d), or was deprecated by Phase 3 alpha decay |

State is persisted in the per-profile `auto_generated_strategies` table with timestamps for every transition and the full validation report JSON.

**Weekly cadence.** `multi_scheduler` runs two new tasks:

- `_task_auto_strategy_generation` (Sundays only): asks the AI for 3 new specs tailored to the profile's recent performance, validates each, moves survivors into shadow mode.
- `_task_auto_strategy_lifecycle` (daily): promotes matured shadows and retires failed ones.

**Safety rails.**
- `max_active_auto_strategies = 5` — hard cap on live auto-strategies per profile
- Spec validation rejects any field or operator outside the allowlist
- Failed validations delete the rendered module file so the registry won't import it
- Shadow strategies contribute zero to capital allocation until promoted
- Every AI-proposed spec with a duplicate name or malformed payload is silently dropped — the AI cannot coerce the system into bad state by misbehaving

**Dashboard.** `Evolving Strategy Library` panel on `/ai` shows per-profile counts by status plus each strategy's lineage (generation number, parent, creation/promotion/retirement timestamps).

**Why This Matters:** Strategy discovery is a full-time job at real funds — teams of PhDs spend months designing, backtesting, and promoting new signals. Our system does it every week, for every profile, with a validation gate that is more rigorous than what most human-designed strategies ever receive. The compounding effect is dramatic: a system that loses its best strategy every year to alpha decay without replacing it slowly dies. A system that proposes and promotes new strategies faster than old ones decay is a system with structurally renewable edge. Combined with Phase 3 monitoring, this is the "self-improving" layer real quant funds dream about but can rarely ship because their legacy infrastructure forbids it.

### 7.12 Specialist AI Ensemble (Phase 8 of Quant Fund Evolution)

One AI looking at everything has systematic blind spots. Real funds run teams of specialists — earnings analysts, technicians, macroeconomists, risk managers — and combine their views. We do the same with focused prompts. Four specialist AIs review every shortlisted candidate in parallel; a meta-coordinator synthesizes their verdicts. See `specialists/` and `ensemble.py`.

**The four specialists:**

| Specialist | Lens | Unique authority |
|---|---|---|
| `earnings_analyst` | Earnings surprise, guidance tone, SEC filing alerts (going concern, material weakness) | — |
| `pattern_recognizer` | Chart structure, breakout quality, momentum confluence, volume confirmation | — |
| `sentiment_narrative` | News flow, political/macro narrative, insider clusters, unusual options flow | — |
| `risk_assessor` | Regime risk, concentration, liquidity, drawdown context, correlation to existing positions | **VETO authority** — can block a trade regardless of the other three |

Each specialist is a small module with `NAME`, `DESCRIPTION`, `build_prompt(candidates, ctx)`, and `parse_response(raw)`. The module never calls the AI itself — `ensemble.run_ensemble()` owns the AI call, retries, and cost accounting.

**Cost scales with specialist count, not candidate count.** Every specialist batches all shortlisted candidates into a single AI call. With 4 specialists on a shortlist of 15 candidates, the ensemble costs 4 AI calls per cycle (not 60).

**Shared across profiles of the same market type (2026-04-17).** The ensemble evaluates candidates, not profiles. An earnings analyst's verdict on AAPL is the same whether the requesting profile has $25K or $500K. Profiles of the same market type (e.g. Mid Cap, Mid Cap 25K, Mid Cap 500K all = "midcap") share one ensemble run per cycle. Only the first profile triggers the AI calls; subsequent profiles reuse the cached verdicts. The batch trade selector remains per-profile because it makes capital-dependent sizing decisions. This optimization reduced AI costs by ~63% ($5.75/day → ~$2.10/day with 10 profiles).

**Synthesis algorithm:**

```
For each candidate:
  buy_score  = Σ weight × (confidence / 100) for specialists voting BUY
  sell_score = Σ weight × (confidence / 100) for specialists voting SELL
  verdicts with confidence < 25 are ignored (specialist abstained)
  if risk_assessor.verdict == "VETO":  → final = VETO (blocks trade)
  elif buy_score > sell_score:        → final = BUY, confidence scaled
  elif sell_score > buy_score:        → final = SELL, confidence scaled
  else:                               → final = HOLD
```

Specialist weights: `pattern_recognizer=1.2`, `earnings_analyst=1.0`, `risk_assessor=1.0`, `sentiment_narrative=0.9`. Pattern gets the highest weight because chart-level evidence is the most concrete; narrative gets the lowest because news flow is noisy. The risk specialist's VETO is binary — fires or not — and supersedes any consensus.

**Pipeline integration.** `trade_pipeline.py` Step 3.7 runs the ensemble between candidate data construction and the final trade-selection AI. Vetoed candidates are dropped from the shortlist entirely (they never reach the final AI call). Surviving candidates carry an `ensemble_summary` field (compact one-liner) that the final AI prompt sees alongside raw indicators and alt-data.

**Safety:** malformed specialist responses parse to empty lists, treated as "specialist abstains" — one broken specialist does not abort the pipeline. Confidence values are clamped to [0, 100] on ingest so a hallucinated "confidence 250" from the AI cannot swing the consensus.

**Dashboard.** `Specialist Ensemble` panel on `/ai` shows per-profile, per-symbol consensus + each specialist's vote + confidence, plus a highlighted list of risk VETOs. Makes it trivial to debug bad trades: which specialist was wrong?

**Why This Matters:** A single generalist prompt rounds off its weaker signals. A specialist forced to focus on exactly one dimension produces sharper verdicts. Combining them with confidence-weighted voting + veto authority is how real institutional research desks decide positions — portfolio managers don't see "the answer," they see each analyst's position and synthesize. Phase 8 brings that structure to AI-first trading at a constant cost of 4 specialist calls per cycle, regardless of shortlist size.

### 7.13 Event-Driven Architecture (Phase 9 of Quant Fund Evolution)

Timers are for batch jobs. Markets react to events. A material 8-K filed at 9:31 shouldn't wait until the 9:45 scan tick to influence the portfolio — we should see it and react within seconds. Phase 9 introduces a lightweight in-process event bus, a set of detectors that watch for real triggers, and handlers that fire immediately. See `event_bus.py`, `event_detectors.py`, and `event_handlers.py`.

**Event bus.** `event_bus.emit(db, type, symbol, severity, payload, dedup_key)` inserts a row into the per-profile `events` table. The UNIQUE constraint on `dedup_key` enforces idempotence — a detector can safely call emit every tick without creating duplicates. `dispatch_pending(db, ctx, limit)` pulls every unhandled event, calls each subscribed handler, records the handler results in `handler_results_json`, and marks the event handled. A handler raising an exception does NOT abort the other handlers.

**Event types** (the closed set handlers can subscribe to):

| Type | Fired by | Default handler(s) |
|---|---|---|
| `sec_filing_detected` | Phase 4 SEC monitor writes a medium/high alert | log_activity + fire_ensemble |
| `earnings_imminent` | Held position has earnings within 24h | log_activity |
| `price_shock` | Held position moves ≥5% on ≥2× volume | log_activity + fire_ensemble |
| `prediction_big_winner` | Resolved prediction with ≥+15% return | log_activity |
| `prediction_big_loser` | Resolved prediction with ≤-15% return | log_activity |
| `strategy_deprecated` | Phase 3 alpha decay deprecates a strategy | log_activity |

**Detectors** (`event_detectors.py`). Each detector is a pure function of database + API state. Every detector uses a dedup key that ties the event to its underlying trigger (SEC filing's accession number, prediction's id, today's date + symbol) so running the tick every 5 minutes doesn't spam the event stream.

**Handlers** (`event_handlers.py`):
- `handler_log_activity` — writes a human-readable row to the profile's activity feed (for email digests and dashboard)
- `handler_fire_ensemble` — runs the Phase 8 specialist ensemble on the event symbol (only for SEC filing + price shock events — the two types where reactive AI analysis justifies the cost)

`register_default_handlers()` wires these up at scheduler startup.

**Scheduler integration.** `_task_event_tick` runs at every scan cycle (15 min): register handlers, run all detectors, dispatch up to 20 pending events. Rate-limiting by dispatch batch prevents an event storm (e.g., sector-wide price shock across 30 held positions) from blocking the scheduler.

**Design: in-process SQLite, not external broker.** We considered Redis / Kafka but SQLite with WAL mode handles hundreds of events per hour trivially and keeps deployment simple. Handler and detector APIs don't depend on the persistence layer — switching to Redis later is a ~100-line change.

**Dashboard.** `Event Stream` panel on `/ai` shows the last 24h of events per profile with severity counts, the event payload (move %, SEC form type, etc), and handler outcomes (e.g., `ensemble=VETO/85%` means the reactive ensemble vetoed).

**Why This Matters:** The gap between "a market-moving event occurred" and "the AI saw it and reacted" is where real alpha comes from in intraday trading. A 15-minute scan tick is a 15-minute information lag — other funds with event-driven architectures trade against you every cycle. Phase 9 brings that lag down to the next scan tick (and eventually, with real-time push sources like WebSocket feeds, to seconds). The specialist ensemble (Phase 8) is the natural reaction engine for events; Phase 9 plugs them into a fast trigger loop.

### 7.14 Cross-Asset Crisis Detection (Phase 10 of Quant Fund Evolution)

Every alpha layer above this one is worth zero if a single regime break wipes out the account. Phase 10 is the capital-preservation backstop. See `crisis_detector.py`, `crisis_state.py`, and `ROADMAP.md`.

**Six monitored signals:**

| Signal | Trigger | Why it matters |
|---|---|---|
| **VIX level** | ≥ 22 elevated, ≥ 32 crisis, ≥ 45 severe | Raw volatility regime |
| **VIX term inversion** | 3M/spot ratio < 0.95 | Front-month stress priced higher than 3M — imminent event |
| **Cross-asset correlation spike** | 10-day avg \|corr\| of SPY/TLT/GLD/UUP ≥ 0.75 | Everything moves together = liquidity crunch |
| **Bond/stock divergence** | TLT up + SPY down with spread ≥ 3% over 5d | Classic flight-to-safety |
| **Gold rally** | GLD ≥ +3% over 5d | Safe-haven demand |
| **Credit stress** | HYG/LQD ratio ≤ -2% over 10d | High-yield bonds under stress relative to investment grade |
| **Event cluster** | ≥ 3 Phase-9 `price_shock` events in 30 min | Regime break in progress across held positions |

**Four crisis levels** with automatic position-sizing responses:

| Level | Trigger | Size multiplier | Pipeline behavior |
|---|---|---|---|
| `normal` | no material signals | 1.0× | Trade as usual |
| `elevated` | VIX > 22, or 1+ signal | 0.5× | Positions sized down |
| `crisis` | VIX > 32, or 3+ signals | 0.0× | New longs blocked; SELL/SHORT allowed |
| `severe` | VIX > 45, or 5+ signals, or critical signal | 0.0× | Liquidate / cash |

The classifier uses VIX level as the primary gate then escalates with signal count (see `_classify_level` for exact rules).

**Persistence.** `crisis_state_history` records one row per transition (not per tick) with `from_level`, `to_level`, signals, readings, and size multiplier. `get_current_level()` returns the most-recent row. `run_crisis_tick()` runs detection, writes a history row only on transition, and emits a `crisis_state_change` event via Phase 9's bus — severity is `critical` on upgrades to severe, proportionate for lesser upgrades, and `info` on downgrades (recovery).

**Pipeline integration.**

1. `_task_crisis_monitor` runs on every scan cycle, before the event tick and before the trade pipeline, so the level is fresh.
2. `_build_market_context()` pulls the current level and constructs a `crisis_context` string injected into the final AI batch prompt (e.g., `CRISIS STATE: ELEVATED (size x0.50). Signals: vix_elevated, bond_stock_divergence. Bias toward capital preservation...`).
3. `trade_pipeline.py` Step 4.9 (crisis gate) applies the hard override AFTER the AI has decided:
   - `elevated`: all `size_pct` values are multiplied by 0.5
   - `crisis` / `severe`: SELL and SHORT orders pass through; BUY orders are removed entirely

**Dashboard.** `Crisis Monitor` panel on `/ai` shows the current level per profile with a colored banner at the top ("⚠ CRISIS" / "⛔ SEVERE"), the active signals and their severities, the current cross-asset readings (VIX, bond/stock deltas, correlation, credit stress), and a collapsible transition history.

**Why This Matters:** The March 2020, September 2008, February 2018, and August 2015 drawdowns were all foreseeable from cross-asset behavior in the preceding days. Funds with discretionary risk desks shrank early; funds trading strictly off single-asset indicators got clipped. Phase 10 codifies the discretionary risk desk: a small set of rules, run every cycle, that automatically disarm the alpha engines when the market says "not today." It is not a return generator. It is the reason a return generator can compound for years without a ruin event.

**This is the final phase of the Quant Fund Evolution roadmap.** With Phases 1-10 live, the system has: proprietary meta-learning (1), rigorous validation (2), decay monitoring (3), alternative data (4, 5), multi-strategy aggregation (6), self-generating strategies (7), specialist ensemble (8), event-driven reaction (9), and crisis-mode capital preservation (10). Each layer compounds with the others — the whole is designed to be structurally harder to compete with than any single clever feature.

### 7.14b Operational Hygiene: AI Cost Tracking + DB Backup

Two small operational layers that don't add alpha but make the system safe to run unattended.

#### AI Cost Ledger

`call_ai()` accepts optional `db_path` and `purpose` arguments. When provided, every AI call is logged to the per-profile `ai_cost_ledger` table with provider, model, input/output token counts, and an estimated USD cost computed from `ai_pricing.PRICING`. Token counts come from each provider SDK's usage object (Anthropic `usage.input_tokens`, OpenAI `usage.prompt_tokens`, Google `usage_metadata.prompt_token_count`).

USD is stored alongside tokens but recomputed from the pricing table on read. This means **re-pricing history is a single-file edit** to `ai_pricing.py` — no DB migration needed when providers change rates.

The dashboard `AI Cost` panel (top of `/ai`) shows per-profile spend over today / 7d / 30d windows plus a breakdown by `purpose` (e.g., `ensemble:earnings_analyst`, `batch_select`, `strategy_proposal`, `sec_diff`) and by `model`. Aggregation is done via `ai_cost_ledger.spend_summary(db_path)`.

**Pricing is approximate.** The `PRICING` table in `ai_pricing.py` is best-effort and treats unknown models with a conservative mid-tier fallback (over-estimate is preferred to silent zero). Treat dashboard totals as order-of-magnitude, not billing-grade.

#### Database Backup

`backup_db.backup_all(project_dir, backup_dir, retain_days=14)` runs every scan cycle's daily snapshot block. For each `*.db` in the project root, it uses SQLite's native `.backup` API (via `conn.backup()`) to produce a WAL-safe consistent snapshot at `/var/backups/quantopsai/<name>.<YYYY-MM-DD>.db`. A plain `cp` of a WAL-mode database can produce a corrupt copy — this never does.

After backup, files older than `retain_days` are pruned. Backups are date-stamped (not numbered) so re-running the task on the same day overwrites atomically (write to `.tmp`, `os.replace`).

**What's protected:** every per-profile DB. These hold all proprietary training data (predictions + features + outcomes), the auto-strategy lifecycle table, the SEC filing alert history, the event ledger, and the crisis state history. The meta-model is useless without them; auto-strategies cannot be "regrown" from scratch because the AI's proposal context depends on the existing performance history. **Lose the DBs and you lose the moat.**

### 7.15 Cross-Phase Integration Test Layer

Every phase has its own unit-test file covering that phase's internals. What those tests can't catch is a regression where two phases *individually* still work but their *composition* is broken — a deprecated strategy that slips back into multi-strategy aggregation, a shadow auto-strategy that accidentally gets promoted to active-trading, a crisis gate that stops blocking new longs after an upstream refactor. See `tests/test_integration.py`.

Integration test coverage:

| Invariant | What it verifies |
|---|---|
| Phase 3 + 6 | Strategies in `deprecated_strategies` are never called by `aggregate_candidates` |
| Phase 6 + 7 | Shadow auto-strategies are reachable via `get_shadow_strategies()` but NOT `get_active_strategies()` |
| Phase 8 + 10 | Crisis gate drops `BUY` actions but preserves `SELL`/`SHORT` regardless of what upstream layers decided |
| Phase 9 + 10 | Crisis state transitions always produce `crisis_state_change` events the event bus can dispatch |
| Phase 2 + 7 | Auto-strategy validation PASS → status `shadow`; FAIL → status `retired` + rendered `.py` file deleted from disk |
| All phases | Every phase's public entry point is importable and its canonical constants (event types, crisis levels, size multipliers, specialist count) match the documented contract |

The `tmp_strategies_dir` fixture in `conftest.py` redirects `STRATEGIES_DIR` per-test so rendering auto-strategy modules in tests never pollutes the real `strategies/` package.

### 7.16 No-Guessing Test Suite

`tests/test_no_guessing.py` (26 tests) enforces that code never uses made-up names for tables, columns, functions, API fields, or template variables. Covers:

| Category | What it catches |
|---|---|
| SQL table names | References to non-existent tables (e.g., `sec_alerts` vs real `sec_filings_history`) |
| Display names | Meta-model features without human-readable labels |
| Template data contracts | View functions building data in wrong shape for templates |
| Function signatures | Calling functions with wrong arguments (e.g., `(profile_id)` vs `(db_path, market_type)`) |
| API field contracts | Python functions returning fields that don't match what template JS expects |
| Template JS validation | JS referencing made-up API fields with blacklist of known bad names |
| render_template kwargs | Templates using variables that the view never passes |
| dotenv loading | Both entry points (scheduler, web) load .env before imports |
| Alpaca-first | No yfinance in equity price paths |

**Why This Matters:** As the system grows, the biggest regression risk isn't individual-phase bugs (unit tests catch those). It's silent breakage of the contracts *between* phases during refactoring — a change that looks local but silently violates a downstream expectation. These integration tests codify the phase-to-phase contracts so a refactor that violates them fails CI loudly, not silently in production.

---

## 8. Intelligence Features

### 8.1 Market Regime Detection (`market_regime.py`)

Classifies the current market using SPY and VIX:

| Regime | Conditions | Strategy Impact |
|---|---|---|
| **Bull** | SPY > SMA50, slope positive, VIX < 20 | Favor momentum, breakouts |
| **Bear** | SPY < SMA50, slope negative, VIX > 25 | Favor shorts, mean reversion, tighter stops |
| **Sideways** | SPY near SMA50, low slope | Tighten stops, reduce position sizes |
| **Volatile** | VIX > 30 and high ATR | Reduce all position sizes, widen stops |

Cached 30 minutes. Injected into AI prompt.

### 8.2 Political Sentiment — MAGA Mode (`political_sentiment.py`)

When enabled, fetches political news from free RSS feeds:
- Google News RSS (trump+market+tariff, political+economy)
- CNBC Economy RSS
- Yahoo Finance (SPY/QQQ/DIA news)

Claude analyzes headlines and returns structured intelligence:
- Volatility level (HIGH/MEDIUM/LOW)
- Is the selloff politically driven or fundamental?
- Expected duration (days/weeks/months)
- **Sector-specific impact** (e.g., tech: negative, defense: positive)
- **Specific ticker mentions** from headlines
- **Trade ideas** with direction and reasoning (e.g., "BA BUY — defense spending likely to increase")
- Recommendation (buy_the_dip / stay_cautious / normal)

MAGA context is lazy-fetched — only when the shortlist is non-empty (zero cost when no trades are candidates). Sector impact is injected per-candidate in the batch prompt so the AI can make stock-specific political decisions.

### 8.3 Earnings Calendar (`earnings_calendar.py`)

Checks yfinance for upcoming earnings dates. Stocks within `avoid_earnings_days` are:
- Skipped in the pre-filter (zero AI cost)
- Flagged in the AI prompt if within 5 days

### 8.4 Per-Stock Memory

Computed dynamically from `ai_predictions` table:
- Tracks win/loss per symbol across all predictions
- Auto-blacklists symbols with 0% win rate after 3+ predictions
- Injects stock-specific history into AI prompt: "You've predicted on RIG 6 times: 0 wins"

### 8.5 Time-of-Day Patterns

Tracks win rate by hour from resolved predictions:
- Identifies best/worst trading hours
- Optional: skip first N minutes after market open
- AI prompt includes current time context

### 8.6 Cross-Profile Learning

Compares performance across user's profiles:
- If Mid Cap wins 64% and Small Cap wins 31%, the AI sees: "Your Mid Cap profile outperforms. Consider being more selective."
- Suggests parameter adjustments based on better-performing profiles

### 8.7 Correlation Management (`correlation.py`)

Before opening a position:
- Fetches 20-day returns for new symbol and all existing positions
- Calculates Pearson correlation
- If correlation > 0.7 with any existing position → reduces size 50%
- Cached 60 minutes

### 8.8 Alternative Data (`alternative_data.py`)

All free from yfinance — no paid subscriptions needed.

| Data Source | What It Provides | Signal Value |
|---|---|---|
| **Insider Transactions** | Recent insider buys/sells, net direction, notable transactions | Insider buying clusters are one of the strongest predictive signals. When a CEO buys $2M of their own stock, they know something. |
| **Short Interest** | Short % of float, days to cover, squeeze risk | High short + positive catalyst = squeeze potential. Short ratio > 5 days = hard to cover quickly. |
| **Options Flow** | Call/put volume, put/call ratio, unusual activity detection | When call volume is 2x+ put volume, smart money is bullish. Unusual volume (>2x open interest) signals imminent move. |
| **Fundamentals** | PE ratio, beta, market cap, sector, industry, insider/institutional ownership % | Context for valuation — is this growth or value? Low PE + insider buying = undervalued. |
| **Intraday Patterns** | VWAP position, opening range breakout, intraday trend, volume profile | Price above VWAP = institutional buyers in control. Opening range breakout predicts trend continuation. |

### 8.9 SEC EDGAR Filings (`sec_filings.py`)

Fetches Form 4 insider filings directly from SEC's free EDGAR RSS feeds. Provides:
- Filing count in last 90 days
- Net signal: insider_buying / insider_selling / mixed
- Links to actual SEC filings

No API key needed. Rate-limited by SEC's User-Agent policy (includes contact email).

### 8.10 Social Sentiment (`social_sentiment.py`)

Scans Reddit via PRAW (official Reddit API wrapper) for ticker mentions:

| Subreddit | What It Captures |
|---|---|
| r/wallstreetbets | High-risk retail momentum (YOLO trades, squeeze plays) |
| r/stocks | More measured stock discussion |
| r/investing | Long-term investment sentiment |
| r/options | Options-specific sentiment and unusual activity |

Features:
- Ticker mention counting with false-positive filtering (excludes common words like "CEO", "IPO", "DD")
- Rough sentiment scoring (bullish/bearish/mixed) from keyword analysis
- Trending detection (5+ mentions = trending)
- Trending tickers discovery (most-mentioned across all subs)

Requires Reddit API credentials (`REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` in .env). Free at reddit.com/prefs/apps. Gracefully degrades if not configured — system works without Reddit data.

---

## 9. Database Schema

### Main Database (`quantopsai.db`)

| Table | Records | Purpose |
|---|---|---|
| `users` | User accounts with encrypted API keys |
| `trading_profiles` | 60+ column config per profile (see §9.1 for v5.0 additions) |
| `user_segment_configs` | Legacy segment configs |
| `decision_log` | Full audit trail per trade decision |
| `activity_log` | Strategy ticker feed |
| `user_api_usage` | Daily AI API call counts |
| `tuning_history` | Self-tuning adjustment records with outcomes |
| `symbol_names` | Cached company names from yfinance |

### 9.1 Schema Additions (v5.0, April 2026)

The autonomous-tuning rollout added 9 schema columns. All are
auto-migrated via the ALTER-TABLE-on-startup framework in
`models.init_user_db()`.

**`trading_profiles` (per-profile autonomy state):**

| Column | Type | Default | Purpose |
|---|---|---|---|
| `signal_weights` | TEXT (JSON) | `'{}'` | Layer 2 — per-signal intensity overrides (4-step ladder) |
| `regime_overrides` | TEXT (JSON) | `'{}'` | Layer 3 — `{param: {regime: value}}` |
| `tod_overrides` | TEXT (JSON) | `'{}'` | Layer 4 — `{param: {tod: value}}` (open/midday/close) |
| `symbol_overrides` | TEXT (JSON) | `'{}'` | Layer 7 — `{param: {symbol: value}}` |
| `prompt_layout` | TEXT (JSON) | `'{}'` | Layer 6 — `{section: verbosity}` |
| `capital_scale` | REAL | `1.0` | Layer 9 — capital allocator's per-profile multiplier (per-Alpaca-account-conserved) |
| `ai_model_auto_tune` | INTEGER | `0` | Per-profile opt-in for tuner-driven AI model A/B testing (cost-sensitive) |

**`users` (per-user autonomy preferences):**

| Column | Type | Default | Purpose |
|---|---|---|---|
| `auto_capital_allocation` | INTEGER | `0` | Layer 9 opt-in — when ON, weekly cron rebalances capital |
| `daily_cost_ceiling_usd` | REAL | NULL | Cost-guard ceiling override; NULL = auto-compute (trailing-7d-avg × 1.5, floor $5) |

**Per-profile DBs gain:**

| Table | Purpose |
|---|---|
| `learned_patterns` | Post-mortem patterns extracted from losing weeks; injected into AI prompt |
| `deprecated_strategies` | Alpha-decay deprecation records with auto-restore tracking |

### Per-Profile Databases (`quantopsai_profile_{id}.db`)

Each profile has an isolated database containing:

| Table | Purpose |
|---|---|
| `trades` | Trade execution log with P&L |
| `signals` | Strategy signals (traded or not) |
| `daily_snapshots` | End-of-day equity snapshots |
| `ai_predictions` | Every AI prediction with resolution status (incl. `features_json` for meta-model training) |
| `deprecated_strategies` | Alpha-decay records (added v5.0) |
| `learned_patterns` | Post-mortem learnings (added v5.0) |

### AI Prediction Resolution

| Prediction | WIN Threshold | LOSS Threshold | Timeout |
|---|---|---|---|
| BUY | Price rises ≥ 5% | Price drops ≥ 3% | 20 trading days → neutral |
| SELL | Price drops ≥ 5% | Price rises ≥ 3% | 20 trading days → neutral |
| HOLD | Change < 3% after 5 days | Change ≥ 3% after 5 days | 20 trading days → neutral |

---

## 10. Web Application

### Technology Stack

| Layer | Technology |
|---|---|
| Framework | Flask 3.x |
| Auth | Flask-Login + bcrypt |
| Templates | Jinja2 + Pico CSS |
| JavaScript | Vanilla JS (no framework) |
| Server | Gunicorn (2 workers) behind nginx |
| Encryption | Fernet (cryptography library) |

### Pages

| Route | Purpose |
|---|---|
| `/dashboard` | Portfolio overview, AI Brain panels (per-profile reasoning + candidate shortlist table with all indicators and alt data), Sector Rotation widget (11 ETFs with inflow/outflow), activity ticker, 15-min countdown timers |
| `/settings` | API keys, profile management (create/edit/delete), strategy sliders |
| `/trades` | Trade history with per-profile filtering |
| `/performance` | 5-tab institutional metrics dashboard (returns, risk, trades, market, scaling) |
| `/ai` | 4-tab AI Intelligence dashboard (brain, strategy, awareness, operations) |
| `/ai-performance` | Legacy AI performance page (redirects to /ai) |
| `/admin` | User management, API usage tracking |
| `/universe/{id}` | Popup showing all symbols in a profile's universe |
| `/backtest/{type}` | Backtest a strategy engine against historical data |

### API Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /api/activity` | Activity feed JSON (for ticker) |
| `GET /api/scheduler-status` | Countdown timer data |
| `GET /api/universe/{id}` | Symbol list with names |
| `POST /api/universe/{id}/cache-names` | Trigger name caching |
| `POST /api/backtest/{id}` | Start background backtest job |
| `GET /api/backtest/status/{job_id}` | Poll backtest job progress |
| `GET /api/slippage-stats/{id}` | Slippage metrics per profile |
| `GET /api/backtest-vs-reality/{id}` | Compare backtest predictions vs actual trades |
| `GET /api/cycle-data/{id}` | Last AI cycle decisions, shortlist, reasoning per profile |
| `GET /api/sector-rotation` | Current sector rotation (11 ETFs, inflow/outflow) |
| `POST /scanning/toggle` | Admin start/stop scanning |
| `GET /api/autonomy-status` (v5.0) | Per-profile snapshot of all active overrides (signal weights, regime/TOD/symbol overrides, prompt layout, capital scale) — labeled-list shape, no raw param keys |
| `GET /api/autonomy-timeline` (v5.0) | Per-profile chronological feed of every autonomous change in last 30 days |
| `GET /api/resolve-param` (v5.0) | Show how a parameter resolves through the override chain right now (for a given profile + param + optional symbol) |
| `GET /api/cost-guard-status` (v5.0) | Today's API spend, daily ceiling (with source label), headroom, 7-day average |
| `GET /api/active-lessons` (v5.0) | Per-profile post-mortem patterns + tuner-detected failure patterns currently in the AI prompt |
| `GET /api/tuning-status` | Per-profile self-tuning readiness pills |
| `GET /api/tuning-history` | Paginated unified history of all autonomous changes |
| `POST /ai/profile/<id>/restore-strategy/<strategy_type>` (v5.0) | Manually restore a deprecated strategy to the active mix |
| `POST /settings/autonomy` (v5.0) | Save user opt-in toggles + cost ceiling override |

---

## 11. Scheduler & Automation

### Task Schedule

| Task | Interval | Scope | Purpose |
|---|---|---|---|
| **Scan & Trade** | 15 min | Per profile within schedule | Screen → Strategy → AI batch → Execute |
| **Check Exits** | 15 min | Per profile within schedule | Stop-loss, take-profit, trailing stops |
| **Cancel Stale Orders** | 15 min | Per profile | Cancel unfilled limit orders > 5 min old |
| **Resolve Predictions** | 60 min | Per profile | Score past AI predictions against actuals |
| **Self-Tune** | Daily (3:55 PM ET) | Per profile | Review adjustments, apply new ones (Layer 1–8 rules) |
| **Daily Snapshot** | Daily (3:55 PM ET) | Per profile | Save equity/cash/positions |
| **Daily Summary Email** | Daily (3:55 PM ET) | Per profile | Portfolio + performance email |
| **Meta-Model Retrain** | Daily (3:55 PM ET) | Per profile | Re-train gradient-boosted classifier on resolved predictions |
| **Alpha-Decay Monitor** | Daily (3:55 PM ET) | Per profile | Detect rolling-Sharpe degradation; deprecate / restore strategies |
| **SEC Filing Monitor** | Daily (3:55 PM ET) | Per market type | Diff new 10-K/10-Q/8-K filings; analyze materiality with AI |
| **Auto-Strategy Lifecycle** | Daily (3:55 PM ET) | Per profile | Promote shadow strategies to active; retire failed ones |
| **DB Backup** | Daily (3:55 PM ET) | Per profile | Rotate proprietary training-data backups |
| **Weekly Digest Email** | Friday 4 PM ET | All profiles (one email) | Cross-profile summary + autonomy activity (added v5.0) |
| **Weekly Capital Rebalance** (v5.0) | Sunday 04:00 UTC | Per user (opt-in) | Layer 9: rebalance per-profile `capital_scale`; respects shared Alpaca accounts |
| **Weekly Post-Mortem** (v5.0) | Sunday | Per profile | Cluster losing-week trades by feature signature; store as `learned_pattern` for AI prompt injection |
| **Auto-Strategy Generation** | Weekly (Sundays) | Per profile | Phase 7: AI proposes new strategy specs |

**File-based idempotency markers** prevent duplicate runs across
scheduler restarts (introduced after a 100-email storm on 2026-04-25).
Markers excluded from `rsync --delete` in `sync.sh`:
`.daily_snapshot_done.marker`, `.daily_summary_sent_p*.marker`,
`.weekly_digest_sent.marker`, `.capital_rebalance_done.marker`,
`.post_mortem_done_p*.marker`.

### External Cron (v5.0)

Daily alt-data refresh runs as a separate cron entry (not via the
QuantOpsAI scheduler — it's a sibling subsystem):

```
0 6 * * * cd /opt/quantopsai-altdata && \
    ALTDATA_BASE=/opt/quantopsai-altdata bash run-altdata-daily.sh \
    >> logs/altdata-$(date +%Y%m%d).log 2>&1
```

Runs sequentially across the 4 standalone projects
(`congresstrades` → `edgar13f` → `biotechevents` → `stocktwits`).
Each project is rate-limit-aware against its upstream API. Total
runtime ~30-50 min depending on new data volume.

### Per-Profile Scheduling

Each profile has its own trading schedule:

| Schedule Type | Hours | Days |
|---|---|---|
| Market Hours | 9:30 AM – 4:00 PM ET | Mon–Fri |
| Extended Hours | 4:00 AM – 8:00 PM ET | Mon–Fri |
| 24/7 | Always | Every day |
| Custom | User-defined start/end | User-selected days |

The scheduler checks `ctx.is_within_schedule()` before processing each profile.

### Process Architecture

```
systemd → quantopsai.service (scheduler)
        → quantopsai-web.service (gunicorn → Flask)
        → nginx (reverse proxy, port 80)
```

Both services auto-restart on failure, start on boot.

---

## 12. External Integrations

### Alpaca Paper Trading API

| Endpoint | Method | Purpose |
|---|---|---|
| `/v2/account` | GET | Equity, buying power, status |
| `/v2/positions` | GET | All open positions |
| `/v2/orders` | POST | Submit market/limit orders |
| `/v2/orders` | GET | List open/filled orders |
| `/v2/orders/{id}` | DELETE | Cancel pending order |

Authentication: Per-profile API key + secret (encrypted in database).

### Yahoo Finance (yfinance)

| Method | Purpose |
|---|---|
| `Ticker.history()` | Daily OHLCV bars (free, no key) |
| `Ticker.calendar` | Earnings dates |
| `Ticker.news` | Headlines |
| `download()` | Batch download multiple symbols |

Used for: All market data, indicators, screener, backtesting, regime detection (SPY/VIX).

### AI Providers

All providers called through `ai_providers.call_ai()` abstraction:

| Provider | SDK | Auth |
|---|---|---|
| Anthropic | `anthropic` | Bearer API key |
| OpenAI | `openai` | Bearer API key |
| Google | `google-generativeai` | API key |

Lazy-imported — missing SDKs don't crash the app, just that provider.

### Resend Email API

| Endpoint | Purpose |
|---|---|
| `POST /emails` | Send HTML emails |

From address: `QuantOpsAI <onboarding@resend.dev>`

### RSS Feeds (Free, No Auth)

| Feed | URL Pattern | Purpose |
|---|---|---|
| Google News | `news.google.com/rss/search?q=...` | Political/market headlines |
| CNBC Economy | `search.cnbc.com/rs/search/...` | Economic news |

---

## 13. Multi-User Security Model

### Credential Storage

All API keys encrypted at rest using Fernet symmetric encryption:
- Encryption key stored in server `.env` file (root-only access)
- Keys decrypted only at runtime when creating API clients
- Never logged, never included in error messages

### Account Isolation

| What | Isolation Method |
|---|---|
| Trades & positions | Per-profile Alpaca accounts (separate API keys) |
| AI predictions & journals | Per-profile SQLite databases |
| Strategy settings | Per-profile rows in `trading_profiles` table |
| Email notifications | Per-user notification_email |

Every database query includes `WHERE user_id = ?` or uses a per-profile database file.

### Authentication

- Email + password with bcrypt hashing
- Flask-Login session cookies
- Admin role for system management

---

## 14. Configuration Reference

### Environment Variables

| Variable | Required | Purpose |
|---|---|---|
| `FLASK_SECRET_KEY` | Yes | Flask session encryption |
| `ENCRYPTION_KEY` | Yes | Fernet key for API key encryption |
| `ALPACA_BASE_URL` | No | Default: `https://paper-api.alpaca.markets` |
| `DB_PATH` | No | Default: `quantopsai.db` |
| `ALTDATA_BASE_PATH` (v5.0) | No | Base path for the 4 standalone alt-data projects. Prod: `/opt/quantopsai-altdata`. Local dev: defaults to `$HOME`. Read by `alternative_data.py` helpers. |
| `RESEND_API_KEY` | No | Email notifications via Resend API |

All other credentials (Alpaca, Anthropic) are stored encrypted in the database per user/profile.

### Per-User Configuration (v5.0)

Stored on the `users` table; surfaced via Settings → Autonomy section:

| Field | Default | Purpose |
|---|---|---|
| `auto_capital_allocation` | OFF | Layer 9 opt-in. When ON, weekly Sunday cron rebalances per-profile `capital_scale` toward proven edge. |
| `daily_cost_ceiling_usd` | NULL (auto) | User-configured daily AI-spend cap. NULL → auto-compute as trailing-7-day-avg × 1.5, floor $5. Cost guard blocks any autonomous action that would push today's spend over this. |

### Per-Profile Configuration (v5.0)

Stored on `trading_profiles`; surfaced via Settings → per-profile form:

| Field | Default | Purpose |
|---|---|---|
| `ai_model_auto_tune` | OFF | Per-profile opt-in. When ON, the tuner is allowed to A/B test alternative AI models for this profile within the cost guard. Off by default because flipping ON can increase API spend. |

### Default Risk Parameters by Market Type

| Parameter | Micro | Small | Mid | Large | Crypto |
|---|---|---|---|---|---|
| Stop Loss | 10% | 6% | 5% | 4% | 8% |
| Take Profit | 15% | 8% | 7% | 6% | 10% |
| Max Position | 5% | 8% | 8% | 7% | 7% |
| Min Volume | 100K | 300K | 500K | 1M | 0 |
| Universe Size | ~60 | ~120 | ~120 | ~100 | ~33 |

---

## 15. Cost Model

### Infrastructure

| Component | Monthly Cost |
|---|---|
| DigitalOcean droplet (1 vCPU, 1GB RAM) | $6 |
| Market data (Yahoo Finance) | Free |
| Trade execution (Alpaca paper) | Free |
| Email notifications (Resend free tier) | Free |
| **Total infrastructure** | **$6/month** |

### AI API Costs (Anthropic Haiku)

| Scenario | Calls/Cycle | Cycles/Day | Daily Cost | Monthly Cost |
|---|---|---|---|---|
| Crypto (24/7, 15-min) | 1-2 | 96 | ~$0.10 | ~$3.00 |
| Mid Cap (market hours) | 1-2 | 26 | ~$0.03 | ~$0.75 |
| Small Cap (market hours) | 1-2 | 26 | ~$0.03 | ~$0.75 |
| MAGA mode (lazy, per shortlist) | 0-1 | varies | ~$0.02 | ~$0.50 |
| **Typical total** | | | **~$0.18** | **~$5** |

The AI-first batch architecture uses 1 AI call per scan cycle (vs 20+ in the old per-symbol review system). MAGA context is only fetched when the shortlist is non-empty.

### Trade Execution Costs (and why we model them at $0)

The system intentionally does not subtract a per-trade commission or
fee from P&L. This is **a deliberate modeling choice that reflects the
real US retail-equity market**, not an oversight. Documented here so
the reasoning is preserved if it ever comes up again.

| Cost | Modeled? | Reality | Magnitude |
|---|---|---|---|
| **Stock commissions** | $0 | All major US retail brokers (Alpaca, Schwab, Fidelity, E*Trade, IBKR Lite, Robinhood) charge $0 commission on stock trades since 2019. Alpaca paper trading matches this exactly. | $0 / trade |
| **SEC fee (Section 31)** | not modeled | Charged on sells only. Currently $8 per $1M proceeds (0.0008%). On a $10K trade, $0.008. | < $0.01 / typical trade |
| **FINRA TAF** | not modeled | Charged on sells only. $0.000166/share, capped at $8.30/trade. A 1,000-share sell pays $0.17. | < $0.20 / typical trade |
| **Bid-ask spread** | **modeled implicitly** | Real cost on every fill. Captured via `slippage_pct` (decision_price vs fill_price recorded on every trade — see §18). The system already learns and reports this. | varies; typically 1-10 bps |
| **Short borrow fees** | not modeled | Annualized 0.25-2% for liquid names; the system rarely holds shorts longer than 1-3 days, so the unaccrued cost is small. | low; matters only on overnight shorts |
| **AI model API cost** | tracked separately | Per-profile `ai_cost_ledger` (see above). Not subtracted from trade P&L because it's a fixed system cost, not a per-trade execution cost. | ~$1.30/day across all 11 profiles |

**Why $0 commissions is the right model:** Adding a fictional $4.95-per-trade commission would make the simulation **less** realistic, not more. The $0 model matches what an actual user would experience opening an account at any major US broker today.

**Why ignoring the regulatory micro-fees is fine:** SEC fee + FINRA TAF combined run ~$0.01-$0.20 on typical trade sizes. Compared to the bid-ask spread (already captured in slippage tracking) which routinely costs 1-10 basis points (i.e. $1-$10 on a $10K trade), regulatory fees are an order of magnitude or two smaller and would not change any signal-vs-noise judgement about strategy quality.

**The cost that does matter — and is already captured:** **slippage**, recorded on every trade as `decision_price`, `fill_price`, and `slippage_pct`. See §18 (Slippage Tracking) for how this feeds back into self-tuning and the institutional performance dashboard.

**Short-borrow accrual on overnight shorts ✅ Shipped 2026-04-27.** New module `short_borrow.py` computes `notional × bps/day × calendar_days_held` at cover time. Default 0.5 bps/day (~1.8% annualized) for general collateral, with per-symbol overrides for known hard-to-borrow names (GME, AMC, BBBY, DJT). `trader.check_exits` cover branch calls `short_borrow.accrue_for_cover(...)` and subtracts the result from `pnl` before logging the cover trade. Same-day covers (held < 1 calendar day) get $0. 9 structural tests in `tests/test_short_borrow.py` including a source-level guard against the integration silently disappearing.

**Source of decision:** user + assistant analysis on 2026-04-27 reviewing today's exits (CHANGELOG entry of same date). The user explicitly recalled E*Trade not charging him for trades and asked for an opinion; my recommendation was "leave commissions/fees at $0, you're right." Both agreed; this section is the record of the reasoning.

---

## 16. Codebase Reference

### File Count & Structure

```
/opt/quantopsai/
├── Strategy Engines (5 files, ~2,300 lines)
│   ├── strategy_micro.py      Micro cap strategies
│   ├── strategy_small.py      Small cap strategies
│   ├── strategy_mid.py        Mid cap strategies
│   ├── strategy_large.py      Large cap strategies
│   └── strategy_crypto.py     Crypto strategies
├── Strategy Infrastructure (3 files, ~1,000 lines)
│   ├── strategy_router.py     Routes to correct engine
│   ├── fallback_strategy.py  Fallback combined strategy (unknown market types)
│   └── strategies.py          Conservative strategies (SMA/RSI)
├── AI & Intelligence (6 files, ~3,000 lines)
│   ├── ai_analyst.py          Multi-model AI analysis
│   ├── ai_providers.py        Provider abstraction layer
│   ├── ai_tracker.py          Prediction tracking & resolution
│   ├── self_tuning.py         Performance feedback & auto-adjustment (Layer 1+8 rules)
│   ├── political_sentiment.py MAGA mode news analysis
│   ├── market_regime.py       Bull/bear/sideways detection
│   ├── news_sentiment.py      News-based sentiment analysis
│   ├── meta_model.py          Phase 1 — gradient-boosted classifier
│   ├── alpha_decay.py         Strategy auto-deprecation / restoration
│   └── post_mortem.py (v5.0)  Losing-week pattern extraction
├── Autonomy Layer Modules (v5.0, ~1,800 lines)
│   ├── param_bounds.py            Declarative PARAM_BOUNDS + clamp()
│   ├── signal_weights.py          Layer 2 — per-signal intensity
│   ├── regime_overrides.py        Layer 3 + the resolve_for_current_regime entry point
│   ├── tod_overrides.py           Layer 4 — per-time-of-day overrides
│   ├── symbol_overrides.py        Layer 7 — per-symbol overrides
│   ├── prompt_layout.py           Layer 6 — adaptive prompt verbosity
│   ├── insight_propagation.py     Layer 5 — cross-profile fan-out
│   ├── capital_allocator.py       Layer 9 — per-Alpaca-account capital allocation
│   └── cost_guard.py              Cross-cutting daily-spend ceiling enforcement
├── Trading & Execution (5 files, ~1,800 lines)
│   ├── trade_pipeline.py      Core trade pipeline (AI-first decision engine)
│   ├── trader.py              Exit management & stop-loss
│   ├── portfolio_manager.py   Position sizing & risk controls
│   ├── correlation.py         Position correlation checking
│   └── earnings_calendar.py   Earnings date lookup
├── Data & Market (3 files, ~800 lines)
│   ├── market_data.py         yfinance wrapper & indicators
│   ├── screener.py            Stock screening & filtering
│   └── segments.py            Market segment definitions
├── Web Application (5 files, ~1,800 lines)
│   ├── app.py                 Flask application factory
│   ├── auth.py                Authentication routes
│   ├── views.py               Dashboard, settings, admin routes
│   ├── templates/             Jinja2 HTML templates (12 files)
│   └── static/                CSS + JavaScript (3 files)
├── Infrastructure (6 files, ~2,000 lines)
│   ├── multi_scheduler.py     Multi-user autonomous scheduler
│   ├── scheduler.py           Legacy single-user scheduler
│   ├── models.py              Database schema & queries
│   ├── journal.py             Trade journal
│   ├── metrics.py             Institutional metrics & SVG charts
│   ├── notifications.py       Email notifications
│   └── config.py              Environment configuration
├── Utilities (5 files, ~600 lines)
│   ├── user_context.py        UserContext dataclass
│   ├── crypto.py              Fernet encryption
│   ├── backtester.py          Strategy backtesting
│   ├── backtest_worker.py     Background thread job runner
│   ├── main.py                CLI entry point
│   └── migrate.py             Database migration
├── Deployment (3 files)
│   ├── deploy.sh              One-command deployment
│   ├── sync.sh                Safe code-only rsync wrapper
│   ├── stop_remote.sh         Stop services
│   └── status_remote.sh       Check service status
└── Documentation
    ├── TECHNICAL_DOCUMENTATION.md     This document (system reference)
    ├── EXECUTIVE_OVERVIEW.md          Plain-English "what is this"
    ├── AI_ARCHITECTURE.md             End-to-end AI agent + autonomy map
    ├── SELF_TUNING.md                 Every tuning rule + signal + safety guard
    ├── AUTONOMOUS_TUNING_PLAN.md      The 9-layer autonomy roadmap
    ├── ALTDATA_INTEGRATION_PLAN.md    The 4 standalone alt-data projects
    ├── ROADMAP.md                     What's next
    ├── CHANGELOG.md                   Every fix, feature, hotfix with rationale
    └── README.md                      Quick orientation

External (not in this repo):
    /opt/quantopsai-altdata/{congresstrades,edgar13f,biotechevents,stocktwits}/
        Standalone data-collection projects with their own SQLite DBs.
        Refreshed daily by /opt/quantopsai-altdata/run-altdata-daily.sh
        via cron at 06:00 UTC. Each project has its own venv +
        requirements.txt; isolated from QuantOpsAI's Python env.
```

**Total (QuantOpsAI proper): ~55 Python files, ~21,000+ lines of code**

---

## 17. What-If Backtesting

### Overview

The settings page includes a "What-If" backtesting feature that lets users test parameter changes against historical market data before applying them live. This runs against **actual historical prices** from Yahoo Finance — it does not require any trade history.

### How It Works

1. User adjusts sliders on the settings page (stop-loss, take-profit, ATR multipliers, strategy toggles, etc.)
2. User clicks **"Backtest These Settings"**
3. System runs the market-specific strategy engine against 90 days of historical data for the **full** symbol universe (no sampling)
4. Results displayed inline as a side-by-side comparison:

```
                    Current Settings    Your Changes     Difference
Win Rate:           35.2%              48.1%            +12.9% ↑
Total Return:       -4.2%              +8.9%            +13.1% ↑
Max Drawdown:       -15.3%             -8.7%            +6.6% ↑
Trades:             42                 28               -14
Sharpe Ratio:       -0.45              0.62             +1.07 ↑
Best Trade:         +8.1%              +12.4%           +4.3% ↑
Worst Trade:        -6.2%              -5.1%            +1.1% ↑
```

5. User clicks **"Apply These Settings"** to save, or **"Discard"** to revert

### Technical Implementation

**Data Caching:**
- Historical price data downloaded via `yf.download()` batch request for full universe
- Cached at module level for 24 hours
- First backtest: ~30 seconds (download + simulation)
- Subsequent backtests with different parameters: ~20 seconds (cached data, only re-run strategy)

**Simulation Engine:**
- Walk-forward day-by-day through historical bars
- Uses the same strategy engine (via `strategy_router.run_strategy()`) as live trading
- Supports ATR-based stops, trailing stops, and fixed % stops
- Respects all strategy toggles (enable/disable individual strategies)
- Calculates: total return, win rate, max drawdown, Sharpe ratio, trade count, best/worst trades

**Parameterization:**
- `backtest_with_params(market_type, params, days)` accepts all UserContext fields as a params dict
- `backtest_comparison(market_type, current_params, new_params, days)` runs both and computes diffs
- API endpoint: `POST /api/backtest/<profile_id>` accepts JSON params, returns comparison JSON

**Key Design Decisions:**
- Full universe (no sampling) for representative results
- 90-day default period — enough data for statistical significance, fast enough to complete in 30 seconds
- Color-coded diffs: green for improvements, red for regressions
- No changes saved until user explicitly clicks "Apply"

---

## 18. Slippage Tracking

### Overview

The system tracks the difference between the price at decision time (when the strategy/AI decided to trade) and the actual fill price from Alpaca. This measures real execution quality.

### How It Works

1. **Decision price** recorded when the order is submitted — this is the price the strategy saw when it generated the signal
2. **Fill price** captured asynchronously — a background task queries Alpaca every 15 minutes for recently filled orders and updates the trade record with `filled_avg_price`
3. **Slippage calculated** as: `(fill_price - decision_price) / decision_price × 100`

### Database Fields (trades table)

| Column | Type | Description |
|---|---|---|
| `decision_price` | REAL | Price when strategy/AI made the decision |
| `fill_price` | REAL | Actual fill price from Alpaca (updated async) |
| `slippage_pct` | REAL | Percentage slippage (positive = worse fill) |

### Metrics Displayed

The AI Performance page shows:
- **Average slippage per trade** (%)
- **Total slippage cost** ($)
- **Worst slippage trade** (symbol and %)
- Only displayed when fill price data is available

### Backtester Slippage Model

The backtester applies simulated slippage:
- **Entry:** 0.2% above current price (buying pushes price up)
- **Exit:** 0.2% below target price (selling pushes price down)
- **Total round-trip:** ~0.4% per trade

This makes backtest results more conservative and closer to real trading.

---

## 19. Backtest vs Reality Comparison

### Overview

Compares what the backtester predicted the strategy would do against what actually happened in live trading. This validates whether the backtest model is reliable.

### How It Works

1. When a profile is selected on the AI Performance page, the system runs a 30-day backtest with that profile's current settings (async, no timeout)
2. Queries actual trades from the same 30-day period
3. Displays a comparison:

```
                    Backtest Predicted    Actual Results    Gap
Win Rate:           48.1%                35.2%             -12.9%
Total Return:       +8.9%                -4.2%             -13.1%
Avg Slippage:       0.2% (simulated)     0.8% (actual)     +0.6%
Trades:             42                   28                -14
```

### Interpreting the Gap

| Gap | Meaning |
|---|---|
| Small gap (<5%) | Backtest model is reliable, strategy performs as predicted |
| Medium gap (5-15%) | Some difference due to slippage, timing, or AI vetoes |
| Large gap (>15%) | Backtest model is unreliable — likely slippage, liquidity, or regime change |

The comparison only appears when there are at least 5 closed trades in the period.

---

## 20. Institutional Performance Dashboard

The system includes a 5-tab performance dashboard at `/performance` (traditional metrics) and a separate 4-tab AI Intelligence dashboard at `/ai` (brain, strategy, awareness, operations). Performance metrics are calculated by `metrics.py` using `calculate_all_metrics()`. AI data is served by dedicated view functions and AJAX API endpoints.

### Tab 1: Executive Summary

| Metric | Formula | Target |
|---|---|---|
| Total Return % | (final_equity - initial) / initial × 100 | Positive |
| Annualized Return % | (1 + total_return)^(365/days) - 1 | 40-80% |
| Net vs Gross Return | Net accounts for slippage | Gap should be small |
| Sharpe Ratio | mean(daily_returns) / std(daily_returns) × √252 | >2.0 |
| Sortino Ratio | mean(daily_returns) / std(negative_returns) × √252 | >2.0 |
| Max Drawdown % | Largest peak-to-trough in equity curve | <20% |
| Calmar Ratio | annualized_return / max_drawdown | >2.0 |

Includes: SVG equity curve chart, monthly returns table with bar chart.

### Tab 2: Risk & Stability

| Metric | Formula | Target |
|---|---|---|
| Annualized Volatility | std(daily_returns) × √252 | Low relative to return |
| VaR (95%) | 5th percentile of trade returns | Know worst expected day |
| CVaR (95%) | Mean of returns worse than VaR | Tail risk measure |
| Max Drawdown Duration | Days peak to recovery | <30 days |
| Rolling 3-Month Return | Trailing 63-day return, monthly | Consistently positive |
| Rolling 6-Month Sharpe | Trailing 126-day Sharpe, monthly | Consistently >1.0 |
| Worst Week/Month/Quarter | Worst periods by P&L | Context for drawdown |

Includes: SVG drawdown chart, SVG rolling Sharpe chart.

### Tab 3: Trade Analytics

| Metric | Formula | Target |
|---|---|---|
| Win Rate | winning / total × 100 | >50% |
| Profit Factor | gross_profits / gross_losses | >1.5 |
| Expectancy | (win_rate × avg_win) - (loss_rate × avg_loss) | Clearly positive |
| Avg Win vs Avg Loss | Dollar and percentage | Avg win > avg loss |
| Win/Loss Ratio | avg_win / abs(avg_loss) | >1.0 |
| Avg Hold Days | Mean trade duration | — |
| Trades per Month | Activity level | — |
| Monthly Win Rate | Profitable months / total months | >60% |

Includes: PnL distribution histogram SVG, streak analysis, best/worst month.

### Tab 4: Market Relationship

| Metric | Formula | Target |
|---|---|---|
| Beta vs S&P 500 | cov(portfolio, SPY) / var(SPY) | <0.5 |
| Alpha | portfolio_return - (beta × SPY_return) | Positive |
| Correlation to SPY | Pearson correlation of daily returns | Low |
| Correlation to QQQ | Same for Nasdaq | Low |
| Correlation to BTC | Same for Bitcoin | Low |
| Net Exposure | (long - short) / equity | — |
| Gross Exposure | (long + short) / equity | — |

SPY/QQQ/BTC data fetched from yfinance with 30-minute cache.

### Tab 5: Scalability

| Metric | Source | Purpose |
|---|---|---|
| Avg Position Size | Trade data | Current trade scale |
| Slippage per Trade | Fill vs decision price | Execution quality |
| Slippage vs Gross Profit | Slippage total / gross profit | Should be <20% |
| Capacity Projection | Position / daily volume ratio | $10K to $1M scaling table |

### Tab 6: AI Intelligence

| Metric | Source | Purpose |
|---|---|---|
| Prediction Win Rate | ai_predictions table | Overall AI accuracy |
| Per-Symbol Track Record | ai_predictions grouped by symbol | Stock-specific AI performance |
| Signal Type Breakdown | ai_predictions grouped by signal | Which signals work (BUY/SELL/HOLD) |
| Confidence Distribution | ai_predictions confidence field | Is higher confidence more accurate? |
| Self-Tuning History | tuning_history table | What adjustments were made and their outcomes |
| Cross-Profile Comparison | All profiles' ai_predictions | Which profile's AI is performing best |

### Chart Generation

All charts are inline SVG generated by `metrics.py`:
- `render_equity_curve_svg()` — line chart with responsive viewBox
- `render_drawdown_svg()` — inverted area chart (red below zero)
- `render_bar_chart_svg()` — colored bars for monthly returns and PnL distribution
- `render_rolling_sharpe_svg()` — line chart of rolling Sharpe stability

No external charting libraries — pure SVG works in all browsers and emails.

### Previous Risk Analysis

The following metrics were previously on the AI Performance page and are now incorporated into the Performance Dashboard:

| Metric | Calculation | Source |
|---|---|---|
| **Max Drawdown** | Largest peak-to-trough decline in equity | `daily_snapshots` table (preferred) or cumulative trade PnL fallback |
| **Value at Risk (95%)** | 5th percentile of all trade returns sorted ascending | Trade PnL as % of cost basis |
| **Worst Single Trade** | Minimum PnL across all closed trades | `trades` table |
| **Worst Day** | Minimum sum of PnL grouped by day | `trades` table grouped by date |
| **Longest Losing Streak** | Maximum consecutive trades with `pnl < 0` | `trades` table ordered by timestamp |
| **Current Streak** | Active winning or losing streak | Last N trades |
| **Longest Winning Streak** | Maximum consecutive trades with `pnl > 0` | `trades` table ordered by timestamp |
| **Avg Losing Streak** | Mean length of all losing streaks | All identified losing streaks |

### Monthly Returns (Consistency)

Trades are grouped by month (from timestamp `YYYY-MM`), sorted most recent first:

| Column | Description |
|---|---|
| Month | Calendar month label (e.g., "Apr 2026") |
| Trades | Total closed trades that month |
| Wins | Trades with `pnl > 0` |
| Losses | Trades with `pnl < 0` |
| P&L | Sum of all trade PnL for the month |
| Return | Monthly PnL as % of starting equity (from `daily_snapshots`) |

### Data Requirements

- Risk metrics require at least 5 closed trades; otherwise "Not enough data" is displayed.
- Max drawdown uses `daily_snapshots` equity values when available; falls back to reconstructing an equity curve from cumulative trade PnL.
- Monthly return % shows 0.0% when no daily snapshot equity data is available for that month.
- All sections respect the per-profile filter dropdown on the AI Performance page.

---

## 21. Tax and Regulatory Considerations

### Current Status

The system is a paper trading platform and does not currently handle tax reporting. The following considerations apply if the system is used with real money:

### Short-Term Capital Gains

All trades held less than 1 year are taxed as ordinary income (not the lower long-term capital gains rate). Since this system is designed for short-term trades (average hold: days to weeks), virtually all profits would be short-term gains. Tax rates range from 10-37% depending on income bracket.

### Wash Sale Rule

The IRS wash sale rule disallows a tax deduction on a loss if you buy the same or "substantially identical" security within 30 days before or after the sale. This system frequently trades the same stocks, which means:
- Losses may not be immediately deductible
- The disallowed loss gets added to the cost basis of the replacement purchase
- The self-tuning system could re-buy a stock it just sold at a loss within days

A wash sale tracking module would need to:
- Track all sells at a loss per symbol
- Check if the same symbol was bought within 30 days before or after
- Adjust cost basis accordingly
- Flag affected trades in the tax report

### Pattern Day Trader (PDT) Rule

If a trader executes 4 or more day trades within 5 business days in a margin account, they're classified as a Pattern Day Trader and must maintain $25,000 minimum equity. This system's 30-minute scan interval means it can open and close positions within the same day.

Implications:
- Accounts under $25K should limit day trades to 3 per 5-day period
- The system should track day trade count and pause if approaching the limit
- Cash accounts (not margin) are exempt from PDT but have settlement delays

### What Would Need to Be Built

| Feature | Purpose | Priority |
|---|---|---|
| Tax lot tracking | Track cost basis per share for accurate gain/loss | High |
| Wash sale detection | Flag trades affected by wash sale rule | High |
| Day trade counter | Track day trades to avoid PDT violation | High |
| Tax report export | Generate IRS-compatible trade report (Form 8949) | Medium |
| Holding period tracking | Distinguish short-term vs long-term gains | Medium |
| Cost basis methods | Support FIFO, LIFO, specific identification | Low |

These features are not currently implemented. Users should consult a tax professional before using this system with real money.

---

## 22. Scaling Roadmap

See `SCALING_PLAN.md` for the complete scaling plan from $10K paper through $1M+ live trading, including what changes at each stage, what breaks at scale, and success criteria for each milestone.

---

*This document describes a paper trading system. No real capital is at risk. The system is designed to test AI-augmented trading strategies across multiple market segments.*
