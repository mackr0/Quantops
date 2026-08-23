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

# Market-order slippage reserve for BUY sizing and cash checks.
# A market buy sized to 100.0% of remaining virtual cash at the
# decision price overdraws the account the moment the fill prints
# higher — p217 went to -$5.05 on 2026-07-14 when FCEL filled
# +46bps over its decision price (observed cohort market-buy
# slippage: FCEL +46bps, MSTR +120bps; the slippage model's
# telemetry prediction was 12.5bps, an underestimate — hence a
# flat floor, not the model). Every cash-vs-cost comparison on the
# buy side must budget cost * (1 + this) for market orders; limit
# orders can't fill above their checked price and skip the reserve.
# 150bps: ABOVE the worst observed fill drift (MSTR 120bps) — a
# reserve below the observed max only shrinks the overdraw, it
# doesn't close it. Cost of the headroom: ~1.5% of one position's
# notional briefly unspent per cash-constrained buy — negligible.
MARKET_BUY_SLIPPAGE_RESERVE_PCT = 0.015

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
# Shadow purpose scope (2026-08-23, docs/25 decision D5): only AI calls
# whose purpose starts with one of these prefixes are shadowed. The
# specialist calls ("ensemble:*") are where same-call shadowing earns
# its keep; the apex batch_select call (78% of tokens) is compared via
# the experiment's live replicates instead. Empty tuple = shadow
# everything (the pre-08-23 behavior). Override with a comma-separated
# SHADOW_PURPOSES env var.
SHADOW_PURPOSES = tuple(
    p.strip() for p in os.getenv("SHADOW_PURPOSES", "ensemble:").split(",")
    if p.strip()
)

# Default trading parameters (overridden per-profile via UserContext)
DEFAULT_MAX_POSITION_PCT = 0.10
DEFAULT_STOP_LOSS_PCT = 0.03
DEFAULT_TAKE_PROFIT_PCT = 0.10
SCREEN_MIN_PRICE = 10.00  # fresh-start baseline floor (institutional: excludes sub-$10 penny/meme tier)
# 2026-07-21 — default aligned to the operator's LIVE band ($10,000;
# every active profile stores this in trading_profiles.max_price, which
# is what the screener actually uses — this constant is only the
# fallback for contexts/profiles created without an explicit value).
# The old default of 20.00 was a legacy small-cap artifact that no live
# profile used; a new profile silently inheriting it would have had its
# universe strangled to sub-$20 names.
SCREEN_MAX_PRICE = 10000.00
SCREEN_MIN_VOLUME = 500000
# Specialist ensemble cost control (2026-06-30): globally disable these
# advisory (non-veto) LLM specialists. They were ~22% of daily AI cost and are
# largely redundant with the free 179-rule deterministic panel. Disabling them
# does NOT affect learning (specialist verdicts aren't a learning feature) and
# does NOT remove any veto/protection (neither is VETO_AUTHORIZED). To
# re-enable, remove from this list. The ensemble keeps a ≥2-active floor.
GLOBALLY_DISABLED_SPECIALISTS = ["sentiment_narrative", "pattern_recognizer"]

# Concentration-aware candidate selection (2026-06-29): annotate each
# candidate with a "book fit" line (max return-correlation to held names +
# same-sector count) in the AI prompt, so the AI proposes trades that
# DIVERSIFY rather than piling onto the cluster the risk specialists would
# veto post-selection. Advisory only (never blocks). Kill switch here.
ENABLE_CONCENTRATION_AWARE = True

# Minimum average daily DOLLAR volume (price * 20-day mean share volume).
# Share-count alone (SCREEN_MIN_VOLUME) doesn't capture tradability: 500k
# shares of a $2 stock is $1M ADV (thin) while 500k shares of a $50 stock is
# $25M (deep). This dollar floor is the institutional "liquid enough to trade"
# gate that excludes the cheap-but-liquid names whose wide spreads whipsaw the
# ATR stops. Operator-set policy — deliberately NOT auto-tuned (see
# self_tuning._OPERATOR_ONLY_PARAMS).
SCREEN_MIN_ADV = 5_000_000
