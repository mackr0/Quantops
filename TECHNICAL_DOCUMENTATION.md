# QuantOpsAI — Technical Documentation

**Version:** 3.0
**Date:** April 11, 2026
**System:** AI-powered autonomous paper trading platform
**Architecture:** Python 3.12 / Flask / SQLite / DigitalOcean

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

---

## 4. AI Analysis Pipeline

### 4.1 Multi-Provider Support

| Provider | Models Available | Pricing Tier |
|---|---|---|
| **Anthropic** | Claude Haiku 4.5 (cheapest), Sonnet 4, Opus 4 | ~$0.0007/call (Haiku) |
| **OpenAI** | GPT-4o-mini, GPT-4o, o3-mini | ~$0.0005/call (mini) |
| **Google** | Gemini 2.0 Flash, Gemini 2.5 Pro | ~$0.0004/call (Flash) |

Users select their provider and model per trading profile. The `ai_providers.py` abstraction handles SDK differences and JSON response cleaning.

### 4.2 AI Prompt Structure

The AI receives a structured prompt containing:

```
1. TECHNICAL DATA (JSON)
   - Current price, SMA20, SMA50, EMA12
   - RSI, MACD (value, signal, histogram)
   - Bollinger Bands (upper, lower, middle)
   - Volume vs 20-day average
   - Last 10 closing prices and volumes

2. CONCISE CONTEXT (4 lines max)
   - Market regime: "BEAR (VIX 25)"
   - Stock-specific history: "Your record on SOFI: 2W/6L, 25% win rate"
   - Overall accuracy: "Your win rate: 45%"
   - Earnings warning (if within 5 days)

3. POLITICAL CONTEXT (if MAGA Mode active)
   - Volatility level, panic-driven assessment
   - Affected sectors, duration estimate

4. RESPONSE SCHEMA
   {signal: BUY|SELL|HOLD, confidence: 0-100,
    reasoning: "...", risk_factors: [...],
    price_targets: {entry, stop_loss, take_profit}}
```

### 4.3 Consensus Mode

When enabled, strong signals (BUY/SELL) are reviewed by a second AI model:

```
Primary Model (Haiku) says BUY →
  Secondary Model (Sonnet) also says BUY →
    CONSENSUS AGREE → confidence boosted 10%, trade proceeds
  Secondary says HOLD/SELL →
    CONSENSUS DISAGREE → signal downgraded to HOLD, no trade
```

This adds ~50% more AI calls but only on actionable signals (not HOLDs).

### 4.4 JSON Response Handling

All AI responses go through `_strip_markdown_fences()` which:
1. Removes ` ```json ``` ` wrappers
2. Extracts the first complete `{...}` JSON object using brace matching
3. Handles preamble text and trailing commentary
4. Works across all providers (Haiku, GPT, Gemini)

---

## 5. Trade Execution Pipeline

The pipeline is designed so AI is **only called when a trade can realistically execute**.

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
  ├─ Route to market-specific engine
  ├─ Run 4 strategies, combine votes
  ├─ MAGA mode override for mean reversion in political panic
  └─ Filter: HOLD → skip, SELL with no position and no shorts → skip

Step 3: AI REVIEW (only for trades that can execute)
  ├─ Build prompt with technical data + concise context
  ├─ Call primary AI model
  ├─ Optional: consensus with secondary model
  ├─ Record prediction to ai_predictions table
  └─ Approve or veto based on confidence threshold

Step 4: EXECUTE
  ├─ ATR-based stop/take-profit calculation
  ├─ Correlation check (reduce size if > 0.7)
  ├─ Position sizing (equity × max_position_pct × signal_strength)
  ├─ Submit order via Alpaca API (market or limit)
  └─ Log trade to journal
```

### Pipeline Efficiency

Logged every cycle: `"27 candidates → 20 post-filter → 5 sent to AI → 1 buy"`

Typical cycle cost: **5-10 AI calls** (not 27+), saving 60-80% on token spend.

---

## 6. Risk Management

### 6.1 Position-Level Controls

| Control | How It Works |
|---|---|
| **ATR-Based Stops** | Stop-loss = entry - (2× ATR), take-profit = entry + (3× ATR). Adapts to each stock's volatility. |
| **Trailing Stops** | Once profitable, stop follows price up (longs) or down (shorts). Trail distance = 1.5× ATR. Never turns a winner into a big loser. |
| **Fixed % Stops** | Fallback when ATR data unavailable. Configurable per profile. |
| **Short-Specific Stops** | Separate wider stops for shorts (default 8%) because upward volatility spikes are sharper. |

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

3. **Cross-profile learning:**
   - If another profile has 20%+ higher win rate → suggest adopting its settings (logged but not auto-applied)

### 7.3 Tuning Memory

Every adjustment is logged with full context in `tuning_history`:

```
| Date       | Parameter              | Old → New  | Win Rate Then | Outcome  |
|------------|------------------------|------------|---------------|----------|
| 2026-04-02 | ai_confidence_threshold| 25 → 70    | 8.8%          | Improved |
| 2026-04-02 | max_position_pct       | 0.10 → 0.08| 8.8%          | Improved |
```

Future adjustments check this history to avoid repeating strategies that already failed.

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

Claude analyzes headlines and assesses:
- Volatility level (HIGH/MEDIUM/LOW)
- Is the selloff politically driven or fundamental?
- Expected duration (days/weeks/months)
- Affected sectors
- Recommendation (buy_the_dip / stay_cautious / normal)

When volatility is politically driven, MAGA Mode overrides HOLD signals to BUY on mean reversion setups — buying political panic dips.

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

---

## 9. Database Schema

### Main Database (`quantopsai.db`)

| Table | Records | Purpose |
|---|---|---|
| `users` | User accounts with encrypted API keys |
| `trading_profiles` | 50+ column config per profile |
| `user_segment_configs` | Legacy segment configs |
| `decision_log` | Full audit trail per trade decision |
| `activity_log` | Strategy ticker feed |
| `user_api_usage` | Daily AI API call counts |
| `tuning_history` | Self-tuning adjustment records with outcomes |
| `symbol_names` | Cached company names from yfinance |

### Per-Profile Databases (`quantopsai_profile_{id}.db`)

Each profile has an isolated database containing:

| Table | Purpose |
|---|---|
| `trades` | Trade execution log with P&L |
| `signals` | Strategy signals (traded or not) |
| `daily_snapshots` | End-of-day equity snapshots |
| `ai_predictions` | Every AI prediction with resolution status |

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
| `/dashboard` | Portfolio overview, per-profile status, activity ticker, countdown timers |
| `/settings` | API keys, profile management (create/edit/delete), strategy sliders |
| `/trades` | Trade history with per-profile filtering |
| `/ai-performance` | Win rate, P&L, prediction accuracy, self-tuning history |
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
| `POST /scanning/toggle` | Admin start/stop scanning |

---

## 11. Scheduler & Automation

### Task Schedule

| Task | Interval | Scope | Purpose |
|---|---|---|---|
| **Scan & Trade** | 30 min | Per profile within schedule | Screen → Strategy → AI → Execute |
| **Check Exits** | 15 min | Per profile within schedule | Stop-loss, take-profit, trailing stops |
| **Cancel Stale Orders** | 15 min | Per profile | Cancel unfilled limit orders > 5 min old |
| **Resolve Predictions** | 60 min | Per profile | Score past AI predictions against actuals |
| **Self-Tune** | Daily (3:55 PM ET) | Per profile | Review adjustments, apply new ones |
| **Daily Snapshot** | Daily (3:55 PM ET) | Per profile | Save equity/cash/positions |
| **Daily Summary Email** | Daily (3:55 PM ET) | Per profile | Portfolio + performance email |

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

All other credentials (Alpaca, Anthropic, Resend) are stored encrypted in the database per user/profile.

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

| Scenario | Calls/Day | Daily Cost | Monthly Cost |
|---|---|---|---|
| 3 profiles, no consensus | ~50 | ~$0.04 | ~$1.20 |
| 3 profiles, with consensus | ~75 | ~$0.06 | ~$1.80 |
| + MAGA mode political analysis | +3 | ~$0.002 | ~$0.06 |
| **Typical total** | **~80** | **~$0.06** | **~$2** |

Pipeline pre-filtering reduces AI calls by 60-80% compared to calling AI on every candidate.

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
│   ├── aggressive_strategy.py Legacy combined strategy
│   └── strategies.py          Conservative strategies (SMA/RSI)
├── AI & Intelligence (6 files, ~3,000 lines)
│   ├── ai_analyst.py          Multi-model AI analysis
│   ├── ai_providers.py        Provider abstraction layer
│   ├── ai_tracker.py          Prediction tracking & resolution
│   ├── self_tuning.py         Performance feedback & auto-adjustment
│   ├── political_sentiment.py MAGA mode news analysis
│   └── market_regime.py       Bull/bear/sideways detection
├── Trading & Execution (5 files, ~1,800 lines)
│   ├── aggressive_trader.py   Trade pipeline & AI review gate
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
│   ├── notifications.py       Email notifications
│   └── config.py              Environment configuration
├── Utilities (5 files, ~600 lines)
│   ├── user_context.py        UserContext dataclass
│   ├── crypto.py              Fernet encryption
│   ├── backtester.py          Strategy backtesting
│   ├── main.py                CLI entry point
│   └── migrate.py             Database migration
├── Deployment (3 files)
│   ├── deploy.sh              One-command deployment
│   ├── stop_remote.sh         Stop services
│   └── status_remote.sh       Check service status
└── Documentation (4 files)
    ├── TECHNICAL_DOCUMENTATION.md  This document
    ├── STRATEGY_DOCUMENT.md        Strategy overview
    ├── STRATEGY_ENGINES_PLAN.md    Engine design plan
    └── SMART_EXECUTION_PLAN.md     Execution feature plan
```

**Total: ~45 Python files, ~17,000+ lines of code**

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

*This document describes a paper trading system. No real capital is at risk. The system is designed to test AI-augmented trading strategies across multiple market segments.*
