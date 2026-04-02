"""Per-user, per-segment configuration context.

Replaces direct reads from config.* globals so that every function in the
pipeline can operate on behalf of any user without touching module-level state.
"""

from dataclasses import dataclass, field
from typing import Optional, List


@dataclass
class UserContext:
    """Carries all per-user, per-segment configuration through the call chain."""
    user_id: int
    segment: str
    display_name: str = ""
    profile_id: Optional[int] = None

    # Alpaca credentials
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_base_url: str = "https://paper-api.alpaca.markets"

    # AI configuration (multi-provider)
    ai_provider: str = "anthropic"
    ai_model: str = "claude-haiku-4-5-20251001"
    ai_api_key: str = ""  # API key for whichever provider

    # Database
    db_path: str = "quantopsai.db"

    # Notifications
    notification_email: str = ""
    resend_api_key: str = ""

    # Risk parameters
    stop_loss_pct: float = 0.03
    take_profit_pct: float = 0.10
    max_position_pct: float = 0.10
    max_total_positions: int = 10

    # AI parameters
    ai_confidence_threshold: int = 25

    # Screener parameters
    min_price: float = 1.0
    max_price: float = 20.0
    min_volume: int = 500_000
    volume_surge_multiplier: float = 2.0

    # RSI thresholds
    rsi_overbought: float = 85.0
    rsi_oversold: float = 25.0

    # Momentum thresholds
    momentum_5d_gain: float = 3.0
    momentum_20d_gain: float = 5.0

    # Breakout threshold
    breakout_volume_threshold: float = 1.0

    # Gap threshold
    gap_pct_threshold: float = 3.0

    # Strategy toggles
    strategy_momentum_breakout: bool = True
    strategy_volume_spike: bool = True
    strategy_mean_reversion: bool = True
    strategy_gap_and_go: bool = True

    # Custom watchlist (additional symbols beyond segment universe)
    custom_watchlist: List[str] = field(default_factory=list)

    # MAGA Mode — factor political volatility into AI analysis
    maga_mode: bool = False

    # Short selling — allow opening short positions on SELL signals
    enable_short_selling: bool = False

    # Self-tuning — AI learns from past wins/losses and adjusts approach
    enable_self_tuning: bool = True

    # Earnings calendar
    avoid_earnings_days: int = 2  # skip stocks with earnings within this many days (0 = don't avoid)

    # Time-of-day patterns
    skip_first_minutes: int = 0  # skip first N minutes after market open (0 = don't skip)

    # Drawdown protection
    drawdown_pause_pct: float = 0.20  # pause all trading at 20% drawdown
    drawdown_reduce_pct: float = 0.10  # reduce position sizes at 10% drawdown

    # Multi-model consensus
    enable_consensus: bool = False
    consensus_model: str = ""  # model ID for secondary opinion, e.g. "gpt-4o-mini"
    consensus_api_key: str = ""  # API key for the secondary model's provider, if different

    # Trading schedule
    schedule_type: str = "market_hours"  # "market_hours", "extended_hours", "24_7", "custom"
    custom_start: str = "09:30"  # HH:MM in ET, only used if schedule_type == "custom"
    custom_end: str = "16:00"    # HH:MM in ET, only used if schedule_type == "custom"
    custom_days: str = "0,1,2,3,4"  # comma-separated day numbers (0=Mon, 6=Sun), only used if custom

    def is_within_schedule(self, now=None):
        """Check if the current time falls within this profile's trading schedule.

        Args:
            now: datetime with timezone (ET). If None, uses current ET time.

        Returns True if the profile should be active right now.
        """
        from zoneinfo import ZoneInfo
        from datetime import datetime

        if now is None:
            now = datetime.now(ZoneInfo("America/New_York"))

        weekday = now.weekday()  # 0=Monday, 6=Sunday
        current_minutes = now.hour * 60 + now.minute

        if self.schedule_type == "24_7":
            return True

        if self.schedule_type == "market_hours":
            # Mon-Fri 9:30 AM - 4:00 PM ET
            if weekday >= 5:
                return False
            return 9 * 60 + 30 <= current_minutes < 16 * 60

        if self.schedule_type == "extended_hours":
            # Mon-Fri 4:00 AM - 8:00 PM ET (pre-market + after-hours)
            if weekday >= 5:
                return False
            return 4 * 60 <= current_minutes < 20 * 60

        if self.schedule_type == "custom":
            # Check day
            allowed_days = [int(d.strip()) for d in self.custom_days.split(",") if d.strip()]
            if weekday not in allowed_days:
                return False
            # Check time
            start_parts = self.custom_start.split(":")
            end_parts = self.custom_end.split(":")
            start_min = int(start_parts[0]) * 60 + int(start_parts[1])
            end_min = int(end_parts[0]) * 60 + int(end_parts[1])
            return start_min <= current_minutes < end_min

        return True  # Unknown schedule type, default to active

    def get_alpaca_api(self):
        """Create an Alpaca REST client for this user."""
        import alpaca_trade_api as tradeapi
        return tradeapi.REST(
            self.alpaca_api_key,
            self.alpaca_secret_key,
            self.alpaca_base_url,
            api_version="v2"
        )

    @property
    def anthropic_api_key(self):
        """Backward compat: return AI key only if provider is Anthropic."""
        return self.ai_api_key if self.ai_provider == "anthropic" else ""

    @property
    def claude_model(self):
        """Backward compat: return AI model only if provider is Anthropic."""
        return self.ai_model if self.ai_provider == "anthropic" else ""

    def get_anthropic_client(self):
        """Create an Anthropic client for this user (backward compat).

        Prefer using ai_providers.call_ai() for new code.
        """
        import anthropic
        key = self.ai_api_key if self.ai_provider == "anthropic" else ""
        if not key:
            raise ValueError("No Anthropic API key configured for this user/profile.")
        return anthropic.Anthropic(api_key=key)


def build_context_from_segment(segment_name: str) -> UserContext:
    """Create a UserContext from the current segments.py + config.py values.

    This provides backward compatibility with the existing single-owner system.
    The returned context uses user_id=1 (the implicit owner).
    """
    import config
    from segments import get_segment

    seg = get_segment(segment_name)

    return UserContext(
        user_id=1,
        segment=segment_name,
        display_name=seg.get("name", segment_name),
        # Alpaca — prefer segment-level keys, fall back to global config
        alpaca_api_key=seg.get("alpaca_key") or config.ALPACA_API_KEY or "",
        alpaca_secret_key=seg.get("alpaca_secret") or config.ALPACA_SECRET_KEY or "",
        alpaca_base_url=config.ALPACA_BASE_URL,
        # AI configuration (defaults to Anthropic for backward compat)
        ai_provider="anthropic",
        ai_model=config.CLAUDE_MODEL,
        ai_api_key=config.ANTHROPIC_API_KEY or "",
        # Database — use the segment-specific DB path
        db_path=seg.get("db_path", config.DB_PATH),
        # Notifications
        notification_email=config.NOTIFICATION_EMAIL or "",
        resend_api_key=config.RESEND_API_KEY or "",
        # Risk parameters from segment definition
        stop_loss_pct=seg.get("stop_loss_pct", config.DEFAULT_STOP_LOSS_PCT),
        take_profit_pct=seg.get("take_profit_pct", config.DEFAULT_TAKE_PROFIT_PCT),
        max_position_pct=seg.get("max_position_pct", config.MAX_POSITION_PCT),
        max_total_positions=config.MAX_TOTAL_POSITIONS,
        # Screener parameters from segment definition
        min_price=seg.get("min_price", config.SCREEN_MIN_PRICE),
        max_price=seg.get("max_price", config.SCREEN_MAX_PRICE),
        min_volume=seg.get("min_volume", config.SCREEN_MIN_VOLUME),
    )
