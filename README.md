# QuantOpsAI

AI-powered autonomous paper trading platform. The AI is the portfolio manager — it sees 33 technical indicators, per-stock news, sector rotation, political sentiment, congressional trades, 13F institutional holdings, biotech milestones, StockTwits sentiment, and its own track record, then picks and sizes the best trades from each scan cycle. Multi-user Flask web app with 5 market-specific strategy engines, a 16-strategy alpha library + auto-generated variants, ensemble specialist AIs, full Phase-2 backtesting gauntlet, alpha-decay auto-deprecation, cross-asset crisis detection, and a **12-layer autonomous self-tuning stack** that adjusts 35+ parameters, signal weights, regime/time-of-day/per-symbol overrides, prompt layout, capital allocation, and AI model selection — all under a daily-cost guard. Runs 24/7 on a cloud server.

## Architecture

```
                       QUANTOPSAI AI-FIRST PIPELINE (per profile)

  Dynamic Universe Discovery (8,000+ symbols via Alpaca)
       |
  Pre-Filter (blacklist, earnings, max positions, drawdown)
       |
  5 Market-Specific Strategy Engines (20 strategies, free — no AI cost)
       |
  Rank Top ~15 Candidates
       |
  SINGLE AI Batch Call — AI sees ALL candidates + portfolio + context:
    - 33 technical indicators per stock
    - Per-stock news headlines
    - Sector rotation (11 ETFs)
    - Relative strength vs sector
    - Market regime (VIX, SPY trend)
    - Political sentiment (MAGA Mode)
    - Per-stock win/loss memory
    - Learned patterns from history
       |
  AI Picks 0-3 Trades & Sizes Them
       |
  Smart Execution (ATR stops, trailing stops, correlation check)
       |
  Pattern Learning (stores regime + strategy type for future analysis)
```

## Key Features

### AI Intelligence (the edge)
- **AI as Portfolio Manager** — One smart batch call per cycle. AI sees the full picture and decides what to trade, not just approve/reject.
- **33 Technical Indicators** — RSI, StochRSI, ADX, MACD, MFI, CMF, OBV, ATR, Bollinger Bands, Keltner Squeeze, VWAP, Fibonacci levels, Pivot Points, 52-week context, ROC, and more.
- **Insider Transaction Data** — Recent insider buys/sells from yfinance + SEC EDGAR Form 4 filings. Insider buying clusters are among the strongest signals in finance.
- **Short Interest & Squeeze Detection** — Short % of float, days to cover, automatic squeeze risk assessment.
- **Options Flow Analysis** — Unusual call/put volume detection, put/call ratio, bullish/bearish flow signals. Shows what smart money is betting on before the move.
- **Intraday Patterns** — Real-time VWAP position, opening range breakout, intraday trend and volume profile from 5-minute bars.
- **Social Sentiment (Reddit)** — Scans r/wallstreetbets, r/stocks, r/investing for ticker mentions, trending detection, and sentiment scoring via PRAW.
- **Per-Stock News** — AI sees recent headlines for every candidate (free from yfinance)
- **Sector Rotation** — Tracks 11 sector ETFs, shows money inflows/outflows, computes relative strength per stock vs its sector
- **Fundamentals** — PE ratio, beta, market cap, sector, industry, institutional/insider ownership percentages
- **Pattern Learning** — Discovers failure/success patterns: "breakouts fail in volatile markets", "mean reversion works midday". Feeds patterns to AI each cycle.
- **MAGA Mode** — Political sentiment with sector-specific impact, ticker mentions, and trade ideas
- **Congressional Trades** — Recent disclosures from `congresstrades` alt-data project (10,500+ trades)
- **13F Institutional Holdings** — Top-fund quarterly holdings from `edgar13f` alt-data project
- **Biotech Milestones** — PDUFA dates and clinical-trial transitions from `biotechevents` alt-data project
- **StockTwits Sentiment** — Retail bullish/bearish daily rollups from `stocktwits` alt-data project
- **Per-Stock Memory** — Tracks win/loss per symbol; auto-blacklists chronic losers
- **Market Regime Detection** — SPY/VIX classifies bull/bear/sideways/volatile
- **12-Layer Autonomous Self-Tuning** — Adjusts 35+ parameters, signal weights, regime/time-of-day/per-symbol overrides, prompt layout, AI model, and capital allocation daily; cross-checked by post-mortems on losing weeks; bounded everywhere by `param_bounds.PARAM_BOUNDS`; gated by a daily AI-cost guard.

### Strategy Engines
- **5 Market-Specific Engines** — Micro Cap, Small Cap, Mid Cap, Large Cap, Crypto (each with 4 dedicated strategies)
- **Dynamic Universe** — Discovers tradable symbols from 8,000+ Alpaca assets (not just hardcoded lists)
- **15-Minute Scan Interval** — Catches intraday momentum that 30-min systems miss

### Risk Management
- **Drawdown Protection** — Reduces size at 10% drawdown, pauses at 20%, auto-resumes at 5%
- **ATR-Based Stops** — Volatility-adapted stop-loss and take-profit per stock
- **Trailing Stops** — Lock in profits as price moves favorably
- **Correlation Management** — Limits correlated positions and sector concentration

### Web Platform
- **Multi-User** — Flask + Flask-Login with bcrypt auth and Fernet-encrypted API keys
- **AI Brain Dashboard** — Shows AI's last decision, reasoning, candidate shortlist with all indicators
- **Sector Rotation Widget** — Live sector ETF inflows/outflows
- **6-Tab Performance Dashboard** — Executive Summary, Risk & Stability, Trade Analytics, Market Relationship, Scalability, AI
- **Active Lessons Widget** — Live view of patterns the system is currently using to gate trades
- **Active Autonomy State** — Snapshot of what every layer has learned and is currently applying
- **Cost Guard Widget** — User-configurable daily AI-spend ceiling with live progress bar
- **Parameter Resolver** — Inspect the override chain (per-symbol → regime → time-of-day → global) for any parameter
- **Autonomy Timeline** — Audit trail of every autonomous adjustment across all 12 layers
- **Indicator Suite Reference** — All 33 indicators grouped by category
- **What-If Backtesting** — Test parameter changes against 90 days of real market data
- **Slippage Tracking** — Decision price vs fill price on every trade

### AI Providers
- Anthropic Claude (Haiku, Sonnet, Opus)
- OpenAI GPT (GPT-4o, GPT-4o-mini)
- Google Gemini (Flash, Pro)

### Cost Efficiency
- **~$0.15-0.25/day** total AI cost (1-2 calls per 15-min cycle, not 20+)
- **$6/month** server (DigitalOcean droplet)
- **Free data** (yfinance, RSS feeds, Alpaca paper trading)

## Setup

### 1. Clone and install

```bash
git clone https://github.com/mackr0/Quantops.git
cd Quantops
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env`:

```
# Alpaca Paper Trading (https://app.alpaca.markets)
SMALLCAP_ALPACA_KEY=your_key
SMALLCAP_ALPACA_SECRET=your_secret
MIDCAP_ALPACA_KEY=your_key
MIDCAP_ALPACA_SECRET=your_secret
CRYPTO_ALPACA_KEY=your_key
CRYPTO_ALPACA_SECRET=your_secret
ALPACA_BASE_URL=https://paper-api.alpaca.markets

# AI Provider (at least one required)
ANTHROPIC_API_KEY=sk-ant-...

# Email notifications via Resend
RESEND_API_KEY=re_...
NOTIFICATION_EMAIL=you@example.com
```

### 3. Initialize database

```bash
python migrate.py
```

### 4. Run locally

```bash
gunicorn --bind 127.0.0.1:5000 "app:create_app()"  # Web UI
python multi_scheduler.py                             # Scheduler (separate terminal)
```

### 5. Run tests

```bash
./run_tests.sh          # All 926 tests
./run_tests.sh -x       # Stop on first failure
./run_tests.sh -k "strategy"  # Strategy tests only
```

### 6. (Optional) Wire alt-data sources

Four standalone projects feed extra signal into the pipeline. Clone each
under `~/` (or set `ALTDATA_BASE_PATH`) and run their `daily` CLI to
populate local SQLite stores:

```bash
~/run-altdata-daily.sh   # one-button: congresstrades + edgar13f + biotechevents + stocktwits
```

QuantOpsAI reads each store read-only via `file:` URI and tolerates
missing DBs — the platform runs fine without them; alt-data signals
just register as `None` until a store appears.

## Cloud Deployment (DigitalOcean)

```bash
./deploy.sh 67.205.155.63     # Full deploy
./sync.sh 67.205.155.63       # Code-only sync (safe)
./status_remote.sh 67.205.155.63
./stop_remote.sh 67.205.155.63
```

## Trading Schedules

| Schedule | Hours | Days |
|---|---|---|
| Market Hours | 9:30 AM - 4:00 PM ET | Mon-Fri |
| Extended Hours | 4:00 AM - 8:00 PM ET | Mon-Fri |
| 24/7 | Always | Every day |
| Custom | User-defined | User-defined |

### Autonomous Tasks

| Interval | Task |
|---|---|
| Every 15 min | Screen -> Strategy -> AI batch select -> Execute |
| Every 15 min | Check exits (stop-loss, take-profit, trailing stops) |
| Every 15 min | Cancel stale limit orders, update fill prices |
| Every 60 min | Resolve AI predictions against actual outcomes |
| Daily 3:55 PM ET | Snapshot, self-tune, pattern analysis, summary email |

## Project Structure

```
Quantops/
├── Strategy Engines
│   ├── strategy_micro.py        Micro cap ($1-$5): volume explosion, penny reversal, breakout, trap avoidance
│   ├── strategy_small.py        Small cap ($5-$20): mean reversion, volume spike, gap & go, momentum
│   ├── strategy_mid.py          Mid cap ($20-$100): sector momentum, breakout, pullback, MACD cross
│   ├── strategy_large.py        Large cap ($50-$500): index correlation, relative strength, dividend, MA alignment
│   ├── strategy_crypto.py       Crypto: BTC correlation, trend following, extreme oversold, volume surge
│   └── strategy_router.py       Routes symbols to correct engine
├── AI & Intelligence
│   ├── ai_analyst.py            AI-first batch trade selection + per-symbol analysis
│   ├── ai_providers.py          Provider abstraction (Anthropic, OpenAI, Google)
│   ├── ai_pricing.py            Per-model USD/M-token rate table
│   ├── ai_cost_ledger.py        Per-profile AI spend ledger + window aggregation
│   ├── ai_tracker.py            Prediction tracking with regime + strategy type
│   ├── self_tuning.py           Pattern learning, auto-adjustment, failure analysis
│   ├── alternative_data.py      Insider trades, short interest, options flow, fundamentals, intraday + 4 alt-data project readers
│   ├── sec_filings.py           SEC EDGAR Form 4 + 10-K/10-Q/8-K semantic analyzer
│   ├── social_sentiment.py      Reddit sentiment via PRAW (r/wallstreetbets, r/stocks)
│   ├── political_sentiment.py   MAGA mode: sector impact, ticker mentions, trade ideas
│   ├── market_regime.py         Bull/bear/sideways/volatile detection
│   ├── earnings_calendar.py     Earnings date checking
│   ├── news_sentiment.py        Per-stock news from yfinance
│   ├── meta_model.py            Gradient-boosted meta-model on past predictions (Phase 1)
│   ├── alpha_decay.py           Rolling-Sharpe decay detection + auto-deprecation (Phase 3)
│   ├── options_oracle.py        IV skew, GEX, max pain, term structure (Phase 5)
│   ├── ensemble.py              Specialist-AI coordinator with VETO (Phase 8)
│   ├── specialists/             4 specialist AIs: earnings, pattern, sentiment, risk
│   ├── event_bus.py             SQLite-backed event bus (Phase 9)
│   ├── event_detectors.py       SEC/earnings/price-shock/big-prediction detectors (Phase 9)
│   ├── event_handlers.py        Event-routed handlers (log + ensemble fire)
│   ├── crisis_detector.py       Cross-asset crisis classifier (Phase 10)
│   └── crisis_state.py          Crisis state transitions + bus emission (Phase 10)
├── Autonomy Layer (12 layers + cost guard)
│   ├── param_bounds.py          PARAM_BOUNDS clamp on every tuned parameter (Layer 1)
│   ├── signal_weights.py        4-step weight ladder for 25 signals (Layer 2)
│   ├── regime_overrides.py      Per-regime parameter overlays (Layer 3)
│   ├── tod_overrides.py         Per-time-of-day parameter overlays (Layer 4)
│   ├── symbol_overrides.py      Per-symbol parameter overlays (Layer 5)
│   ├── prompt_layout.py         AI-prompt section ordering + presence (Layer 6)
│   ├── insight_propagation.py   Lessons learned → in-flight prompt injection
│   ├── capital_allocator.py     Per-Alpaca-account-conserving allocation (Layer 9)
│   ├── post_mortem.py           Closed-loop losing-week + false-negative analysis
│   └── cost_guard.py            User-configurable daily AI-spend ceiling
├── Trading & Execution
│   ├── trade_pipeline.py        AI-first pipeline: pre-filter -> strategy -> rank -> AI batch -> execute
│   ├── trader.py                Exit management, stop-loss, trailing stops
│   ├── portfolio_manager.py     Position sizing, drawdown protection, ATR stops
│   └── correlation.py           Position correlation checking
├── Data
│   ├── market_data.py           33 technical indicators, sector rotation, relative strength
│   ├── screener.py              Dynamic universe discovery (8000+ symbols) + price/volume screening
│   └── segments.py              Fallback hardcoded universes per market type
├── Web Application
│   ├── app.py                   Flask factory with Flask-Login
│   ├── auth.py                  Authentication routes
│   ├── views.py                 Dashboard, settings, performance, API endpoints
│   ├── metrics.py               Institutional metrics calculator + SVG charts
│   ├── templates/               AI Brain panels, sector rotation, candidate shortlist, 6-tab performance
│   └── static/                  CSS + JavaScript
├── Infrastructure
│   ├── multi_scheduler.py       15-min scan, multi-profile, dynamic universe
│   ├── models.py                Database schema, migrations, user/profile CRUD
│   ├── user_context.py          UserContext dataclass (53 fields)
│   ├── journal.py               Per-profile trade journal with pattern learning columns
│   ├── backtester.py            Walk-forward backtester with slippage
│   ├── backtest_worker.py       Background thread job runner
│   ├── notifications.py         Email via Resend API
│   ├── crypto.py                Fernet encryption for API keys
│   └── config.py                Environment configuration
├── Testing
│   ├── tests/                   926 tests (imports, database, strategies, pipeline, web, autonomy, alt-data, structural guardrails)
│   ├── run_tests.sh             Test runner script
│   ├── run_backtest_validation.py  Backtest all 5 engines against real data
│   └── pytest.ini               Test configuration
├── Deployment
│   ├── deploy.sh                Full deployment script
│   ├── sync.sh                  Safe code-only rsync
│   ├── migrate.py               Database migration (idempotent)
│   ├── status_remote.sh         Check service status
│   └── stop_remote.sh           Stop services
└── Documentation
    ├── EXECUTIVE_OVERVIEW.md         Top-down summary for partners / non-technical readers
    ├── TECHNICAL_DOCUMENTATION.md    Complete system documentation (v5.0, 22 sections)
    ├── ROADMAP.md                    10-phase quant-fund evolution + completion log
    ├── AI_ARCHITECTURE.md            All AI signal sources + how they reach the prompt
    ├── SELF_TUNING.md                Self-tuning + 12-layer autonomy reference
    ├── AUTONOMOUS_TUNING_PLAN.md     The 12-wave autonomy rollout plan + status
    ├── ALTDATA_INTEGRATION_PLAN.md   How the 4 alt-data projects plug into the pipeline
    ├── SCALING_PLAN.md               $10K paper -> $1M+ live roadmap
    ├── MONTHLY_REVIEW.md             Operational review template
    ├── CHANGELOG.md                  Per-day change log (enforced by pre-commit hook)
    └── requirements.txt              Python dependencies
```

## Disclaimer

This is for **educational and paper trading purposes only**. No real capital is at risk. AI analysis is probabilistic and can be wrong. Past performance does not guarantee future results.
