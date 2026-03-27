# Quantops

AI-powered autonomous paper trading system for small-cap and micro-cap stocks. Uses Claude AI to review every trade before execution, tracks AI prediction accuracy over time, and runs 24/7 on a cloud server.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                   QUANTOPS PIPELINE                      │
│                                                          │
│  Screen 8,000+ stocks                                    │
│       ↓                                                  │
│  Filter: $1-$20, 500K+ volume                           │
│       ↓                                                  │
│  4 Aggressive Strategies (technical analysis)            │
│       ↓                                                  │
│  Claude AI Review (approve/veto each trade)              │
│       ↓                                                  │
│  Execute on Alpaca (paper trading)                       │
│       ↓                                                  │
│  Track AI accuracy + email notifications                 │
└─────────────────────────────────────────────────────────┘
```

## Features

- **Stock Screener** — Scans 8,000+ tradable symbols via Alpaca snapshots, filters by price range and volume
- **4 Aggressive Strategies** — Momentum breakout, volume spike, mean reversion, gap-and-go
- **AI Trade Review** — Claude analyzes technicals before every trade; vetoes bad ones
- **AI Accuracy Tracking** — Records every AI prediction, resolves against actual prices, reports win rate by confidence band
- **Risk Management** — Position sizing, portfolio constraints, stop-loss (3%), take-profit (10%)
- **Trade Journal** — SQLite database logging every trade, signal, and AI reasoning
- **Autonomous Scheduler** — Runs during market hours on a cloud server, no human needed
- **Email Notifications** — Trade alerts, AI veto alerts, stop-loss triggers, daily summaries
- **Rich Dashboard** — Terminal UI with colored tables and panels
- **Backtesting** — Walk-forward backtester with Sharpe ratio, drawdown, win rate

## Setup

### 1. Clone and install

```bash
git clone https://github.com/mackr0/Quantops.git
cd Quantops
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure API keys

```bash
cp .env.example .env
```

Edit `.env` with your keys:

```
# Alpaca Paper Trading (https://app.alpaca.markets)
ALPACA_API_KEY=your_key
ALPACA_SECRET_KEY=your_secret
ALPACA_BASE_URL=https://paper-api.alpaca.markets

# Anthropic Claude API (https://console.anthropic.com)
ANTHROPIC_API_KEY=sk-ant-...

# Email notifications (Gmail with app password)
SMTP_USER=your@gmail.com
SMTP_PASSWORD=your_app_password
NOTIFICATION_EMAIL=your@email.com
```

**Getting a Gmail App Password:**
1. Go to https://myaccount.google.com/apppasswords
2. Generate a new app password for "Mail"
3. Use that 16-character password as `SMTP_PASSWORD`

### 3. Test the connection

```bash
python main.py account
```

## Commands

### Portfolio & Account

| Command | Description |
|---|---|
| `python main.py account` | Show account info (equity, cash, buying power) |
| `python main.py positions` | Show all open positions |
| `python main.py dashboard` | Rich terminal dashboard with positions, risk summary |

### Technical Analysis

| Command | Description |
|---|---|
| `python main.py analyze AAPL` | SMA crossover + RSI + combined signal |
| `python main.py ai-analyze AAPL` | Claude AI analysis with confidence and risk factors |
| `python main.py sentiment AAPL` | News sentiment analysis using Claude |
| `python main.py scan` | Scan default watchlist with technical signals |
| `python main.py ai-scan` | Full AI + sentiment scan of watchlist |

### Aggressive Small-Cap Trading

| Command | Description |
|---|---|
| `python main.py screen` | Screen 8,000+ stocks for small/micro-cap candidates |
| `python main.py aggro-analyze SYM` | Run 4 aggressive strategies on a symbol |
| `python main.py aggro-scan` | Screen + aggressive analysis on all candidates |
| `python main.py aggro-trade` | Screen + AI review + auto-execute paper trades |

### Trading

| Command | Description |
|---|---|
| `python main.py trade AAPL` | Execute trade based on combined strategy |
| `python main.py trade-scan` | Scan watchlist and trade all signals |
| `python main.py check-exits` | Check stop-loss / take-profit triggers |

### Backtesting

| Command | Description |
|---|---|
| `python main.py backtest AAPL` | Backtest combined strategy (default 365 days) |
| `python main.py backtest AAPL 180` | Backtest with custom day count |

### Journal & Performance

| Command | Description |
|---|---|
| `python main.py journal` | Show all trade history |
| `python main.py journal AAPL` | Show trade history for a specific symbol |
| `python main.py performance` | Show overall performance summary |
| `python main.py snapshot` | Save daily portfolio snapshot |

### AI Performance Tracking

| Command | Description |
|---|---|
| `python main.py ai-report` | Show AI prediction accuracy report |
| `python main.py ai-resolve` | Resolve pending predictions vs actual prices |

## Strategies

### Conservative (Default Watchlist)

| Strategy | Buy Signal | Sell Signal |
|---|---|---|
| SMA Crossover | SMA20 crosses above SMA50 | SMA20 crosses below SMA50 |
| RSI | RSI < 30 (oversold) | RSI > 70 (overbought) |
| Combined | Both agree = STRONG signal | Mixed = WEAK signal |

### Aggressive (Small-Cap)

| Strategy | Buy Signal | Sell Signal |
|---|---|---|
| Momentum Breakout | Price breaks 20-day high + 1.5x volume + RSI 50-80 | Price below 10-day low or RSI > 85 |
| Volume Spike | Volume > 2x avg + price up > 2% + RSI < 70 | Volume fades + 2 red candles |
| Mean Reversion | RSI < 25 + price > 10% below SMA20 | Price returns to SMA20 or RSI > 60 |
| Gap and Go | Open > 3% above prev close + above avg volume | Price drops below today's open |

The aggressive combined strategy scores each sub-strategy (+1 BUY, -1 SELL) and maps to signal strength:
- Score >= 2: STRONG_BUY
- Score 1: BUY
- Score -1: SELL
- Score <= -2: STRONG_SELL

### AI Review Gate

Before any aggressive trade executes, Claude AI analyzes the stock:
- Reviews technicals (SMA, RSI, MACD, Bollinger Bands, volume)
- Provides signal, confidence (0-100), reasoning, risk factors, and price targets
- **Veto rules:** AI SELL on a BUY = vetoed. AI confidence < 40 = vetoed. AI strongly BUY on a SELL = vetoed.
- Every AI prediction is recorded and scored against actual outcomes

### Risk Management

| Parameter | Value |
|---|---|
| Max position size (aggressive) | 10% of equity |
| Max total positions | 10 |
| Stop-loss (aggressive) | 3% |
| Take-profit (aggressive) | 10% |
| Max position size (conservative) | 5% of equity |
| Stop-loss (conservative) | 5% |
| Take-profit (conservative) | 15% |

## Cloud Deployment (DigitalOcean)

The bot runs autonomously on a $6/mo DigitalOcean droplet.

### Deploy

```bash
./deploy.sh 67.205.155.63
```

This will:
1. Install Python and dependencies on the droplet
2. Sync all code and `.env` to `/opt/quantops`
3. Create a systemd service that auto-starts on boot
4. Start the scheduler

### Manage

```bash
# Check status and recent logs
./status_remote.sh

# Stop the bot
./stop_remote.sh

# Redeploy after code changes
./deploy.sh
```

### Remote commands

```bash
# Portfolio dashboard
ssh root@67.205.155.63 "cd /opt/quantops && venv/bin/python3 main.py dashboard"

# AI accuracy report
ssh root@67.205.155.63 "cd /opt/quantops && venv/bin/python3 main.py ai-report"

# Trade history
ssh root@67.205.155.63 "cd /opt/quantops && venv/bin/python3 main.py journal"

# View today's logs
ssh root@67.205.155.63 "tail -50 /opt/quantops/logs/quantops_$(date +%Y-%m-%d).log"
```

### Autonomous Schedule

During market hours (9:30 AM - 4:00 PM ET, Mon-Fri):

| Interval | Task |
|---|---|
| Every 15 min | Check stop-loss / take-profit on all positions |
| Every 30 min | Screen → Analyze → AI Review → Trade |
| Every 60 min | Resolve AI predictions against actual prices |
| 3:55 PM ET | Save daily snapshot + send daily summary email |

Outside market hours, the bot sleeps and automatically wakes at next market open.

## Email Notifications

You'll receive emails for:
- **Trade executed** — Symbol, qty, price, AI analysis, account snapshot, positions
- **AI veto** — What technical said vs what AI said and why
- **Stop-loss / take-profit triggered** — Exit details and P&L
- **Daily summary** — Full portfolio overview, today's trades, AI performance stats

## Project Structure

```
Quantops/
├── main.py                 # CLI entry point with all commands
├── config.py               # Configuration and environment variables
├── client.py               # Alpaca API client wrapper
├── market_data.py          # Historical bars and technical indicators
├── strategies.py           # Conservative strategies (SMA, RSI, combined)
├── aggressive_strategy.py  # Small-cap strategies (momentum, volume, gap)
├── trader.py               # Trade execution with risk management
├── aggressive_trader.py    # Aggressive execution with AI review gate
├── screener.py             # Small/micro-cap stock screener
├── ai_analyst.py           # Claude AI analysis integration
├── news_sentiment.py       # News fetching and AI sentiment scoring
├── ai_tracker.py           # AI prediction accuracy tracking
├── portfolio_manager.py    # Position sizing and risk constraints
├── journal.py              # SQLite trade journal
├── dashboard.py            # Rich terminal dashboard
├── notifications.py        # Email notification system
├── backtester.py           # Walk-forward backtesting engine
├── scheduler.py            # Autonomous market-hours scheduler
├── deploy.sh               # One-command cloud deployment
├── status_remote.sh        # Check remote bot status
├── stop_remote.sh          # Stop remote bot
├── requirements.txt        # Python dependencies
└── .env.example            # Environment variable template
```

## Disclaimer

This is for **educational and paper trading purposes only**. Do not use for real trading without thorough testing and understanding of the risks involved. Past performance does not guarantee future results. AI analysis is probabilistic and can be wrong.
