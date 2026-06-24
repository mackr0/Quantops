import os
from dotenv import load_dotenv

load_dotenv()

# Alpaca
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

# Anthropic / Claude
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

# AI provider failover keys — when set, the circuit breaker in
# `provider_circuit.py` automatically routes calls to these providers
# if the primary (per-profile) provider's circuit opens (3 consecutive
# 5xx/timeout failures). Optional — failover degrades gracefully to
# "no fallback available" when these are unset.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
# 2026-06-24 — the same-provider fallback model. Was "gemini-2.0-flash", which
# Google has DEPRECATED (live calls 404 "model no longer available"), so when
# the per-profile primary (gemini-2.5-flash-lite) circuit-opens — it currently
# trips on "high demand" overload — failover hit a dead model and the AI
# returned 0 trades. gemini-2.5-flash is verified live (HTTP 200) and is the
# natural one-step-up fallback from the flash-lite primary.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# Database
DB_PATH = os.getenv("DB_PATH", "quantopsai.db")

# Risk management
MAX_POSITION_PCT = 0.05
MAX_TOTAL_POSITIONS = 10
DEFAULT_STOP_LOSS_PCT = 0.05
DEFAULT_TAKE_PROFIT_PCT = 0.15

# Watchlist
WATCHLIST = ["AAPL", "MSFT", "GOOGL", "AMZN", "TSLA", "NVDA", "META", "SPY", "QQQ"]

# Email notifications (Resend)
RESEND_API_KEY = os.getenv("RESEND_API_KEY")
NOTIFICATION_EMAIL = os.getenv("NOTIFICATION_EMAIL", "mack@mackenziesmith.com")

# Shadow model evaluation — separate daily cost cap (USD) so shadow
# traffic can never blow out the operational AI budget. Enforced per
# shadow call. Default is intentionally tiny since shadow models are
# the cheap tier (Gemini Flash-Lite, DeepSeek, GPT-4.1 Nano).
SHADOW_DAILY_COST_CAP_USD = float(os.getenv("SHADOW_DAILY_COST_CAP_USD", "1.0"))

# Default trading parameters (overridden per-profile via UserContext)
DEFAULT_MAX_POSITION_PCT = 0.10
DEFAULT_STOP_LOSS_PCT = 0.03
DEFAULT_TAKE_PROFIT_PCT = 0.10
SCREEN_MIN_PRICE = 1.00
SCREEN_MAX_PRICE = 20.00
SCREEN_MIN_VOLUME = 500000
