# Alpaca AI Trader

Paper trading bot using Alpaca API with technical analysis strategies.

## Features

- **SMA Crossover Strategy** — Moving average crossover signals
- **RSI Strategy** — Mean reversion based on RSI levels
- **Combined Strategy** — Merges multiple indicators for stronger signals
- **Automated Trading** — Execute paper trades based on signals
- **Watchlist Scanning** — Scan multiple symbols at once

## Setup

1. Create a free account at [Alpaca](https://alpaca.markets)
2. Get your Paper Trading API keys from the dashboard
3. Install dependencies and configure:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your API keys
```

## Usage

```bash
# View account info
python main.py account

# View current positions
python main.py positions

# Analyze a single stock
python main.py analyze AAPL

# Scan the default watchlist
python main.py scan

# Analyze and trade a single stock (paper)
python main.py trade AAPL

# Scan watchlist and trade all signals (paper)
python main.py trade-scan
```

## Strategies

| Strategy | Signal | Description |
|----------|--------|-------------|
| SMA Crossover | BUY/SELL | Short-term SMA crosses long-term SMA |
| RSI | BUY/SELL | RSI below 30 (oversold) or above 70 (overbought) |
| Combined | STRONG/WEAK BUY/SELL | Both indicators must agree for strong signals |

## Disclaimer

This is for **educational and paper trading purposes only**. Do not use for real trading without thorough testing and understanding of the risks involved. Past performance does not guarantee future results.
