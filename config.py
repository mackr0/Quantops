import os
from dotenv import load_dotenv

load_dotenv()

# Alpaca
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

# Anthropic / Claude
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")

# Database
DB_PATH = os.getenv("DB_PATH", "quantops.db")

# Risk management
MAX_POSITION_PCT = 0.05
MAX_TOTAL_POSITIONS = 10
DEFAULT_STOP_LOSS_PCT = 0.05
DEFAULT_TAKE_PROFIT_PCT = 0.15

# Watchlist
WATCHLIST = ["AAPL", "MSFT", "GOOGL", "AMZN", "TSLA", "NVDA", "META", "SPY", "QQQ"]

# Aggressive strategy settings
AGGRESSIVE_MAX_POSITION_PCT = 0.10
AGGRESSIVE_STOP_LOSS_PCT = 0.03
AGGRESSIVE_TAKE_PROFIT_PCT = 0.10
SCREEN_MIN_PRICE = 1.00
SCREEN_MAX_PRICE = 20.00
SCREEN_MIN_VOLUME = 500000
