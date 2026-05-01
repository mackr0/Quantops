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

    # Short selling — allow opening short positions on SHORT signals
    enable_short_selling: bool = False
    short_stop_loss_pct: float = 0.08  # wider stop for shorts (8% vs 3% for longs)
    short_take_profit_pct: float = 0.08  # shorts profit faster on hard drops
    # Phase 1.5+1.6 of LONG_SHORT_PLAN.md
    # short_max_hold_days: cover any short older than this many calendar
    # days regardless of P&L. Shorts that don't move down quickly are
    # paying borrow + compounding the wrong-thesis risk; close them.
    # short_max_position_pct: cap on individual short position as fraction
    # of equity. Defaults to half of max_position_pct because unlimited
    # downside on shorts means smaller per-name sizing is the standard
    # professional convention.
    short_max_hold_days: int = 10
    short_max_position_pct: Optional[float] = None  # None → derived as max_position_pct / 2

    # P2.2 of LONG_SHORT_PLAN.md
    # target_short_pct: target fraction of GROSS exposure that should
    # be short. 0.0 = long-only (default for all existing profiles —
    # backward-compat). 0.5 = balanced long/short. 0.7 = short-dominant
    # (e.g. profile_10 "Small Cap Shorts" should run heavier short).
    # The AI prompt sees current_short_pct vs target_short_pct on every
    # batch decision and is told to bias toward the underweight side.
    target_short_pct: float = 0.0

    # P4.1 of LONG_SHORT_PLAN.md — beta-targeted construction.
    # target_book_beta: target gross-weighted book beta vs SPY. None
    # = no target (existing behavior, no AI prompt directive). Pro
    # long/short funds typically target book beta of 0.0 (market-
    # neutral) to 0.5 (low net beta). Setting >1.0 is unusual.
    # The AI prompt surfaces current_book_beta vs target_book_beta
    # on each cycle so the AI can bias toward defensive (low-beta)
    # or levered (high-beta) picks to close the gap.
    target_book_beta: Optional[float] = None

    # OPTIONS_PROGRAM_PLAN Phase A2 — Greeks exposure gates. Defaults
    # are conservative; tunable per profile. None = no gate (the
    # behavior pre-Phase-A2).
    # max_net_options_delta_pct: |options-only delta| / equity cap.
    #   0.05 = 5%. Stops the AI from accumulating directional exposure
    #   via options (e.g. stacking long calls until the book is +50%
    #   delta levered).
    max_net_options_delta_pct: Optional[float] = 0.05
    # max_theta_burn_dollars_per_day: positive number = max $/day of
    #   premium decay we're willing to pay (long-vol books). When net
    #   theta is BELOW -limit, block new long-premium trades.
    #   None = no gate; 0 = forbid net long-vol; +X = allow up to $X/day.
    max_theta_burn_dollars_per_day: Optional[float] = 50.0
    # max_short_vega_dollars: cap on short vega exposure. When net
    #   vega is BELOW -limit, block new short-premium trades. Protects
    #   against vol spikes wiping a short-vol book.
    max_short_vega_dollars: Optional[float] = 500.0

    # OPTIONS_PROGRAM_PLAN Phase C3 — wheel automation. Empty list =
    # wheel disabled (default). Comma- or list-form symbols opt the
    # profile into the wheel cycle on those underlyings: cash → CSP →
    # (assigned) → shares → CC → (called away) → cash. Recommendations
    # surfaced via the AI prompt so the user / AI confirms each
    # cycle step rather than auto-fire.
    wheel_symbols: List[str] = field(default_factory=list)

    # Self-tuning — AI learns from past wins/losses and adjusts approach
    enable_self_tuning: bool = True

    # Earnings calendar
    avoid_earnings_days: int = 2  # skip stocks with earnings within this many days (0 = don't avoid)

    # Time-of-day patterns
    skip_first_minutes: int = 0  # skip first N minutes after market open (0 = don't skip)

    # Drawdown protection
    drawdown_pause_pct: float = 0.20  # pause all trading at 20% drawdown
    drawdown_reduce_pct: float = 0.10  # reduce position sizes at 10% drawdown

    # ATR-based stops
    use_atr_stops: bool = True
    atr_multiplier_sl: float = 2.0
    atr_multiplier_tp: float = 3.0

    # Trailing stops
    use_trailing_stops: bool = True
    trailing_atr_multiplier: float = 1.5

    # Conviction-based take-profit override
    # When a long position hits its fixed take-profit threshold, normally
    # we sell and realize the gain. If this override is on, we instead
    # *skip* the fixed TP and let the trailing stop manage the exit —
    # provided the AI still has high conviction AND the trend is intact
    # (ADX >= threshold) AND price is making new highs. Designed for
    # runaway winners like IONQ where fixed TP caps the upside.
    use_conviction_tp_override: bool = False
    conviction_tp_min_confidence: float = 70.0     # AI confidence >= this
    conviction_tp_min_adx: float = 25.0            # trend strength (ADX) >= this

    # Virtual account layer
    is_virtual: bool = False
    initial_capital: float = 100000.0

    # Lever 2 + Lever 3 of COST_AND_QUALITY_LEVERS_PLAN.md.
    # disabled_specialists: JSON list of specialist names whose API
    # call is skipped (e.g. ["pattern_recognizer"]). Updated by the
    # daily _task_specialist_health_check based on calibrator slope.
    # Read by ensemble.run_ensemble via getattr — must be on
    # UserContext or the disable list is silently ignored.
    # meta_pregate_threshold: candidates with meta_prob below this
    # are dropped before the ensemble fires. 0.0 = disabled.
    disabled_specialists: str = "[]"
    meta_pregate_threshold: float = 0.5

    # Layer storage JSON columns. These ARE accessed by
    # `getattr(ctx, X, ...)` from self_tuning.py and ai_analyst.py.
    # Without the field on UserContext, the ctx access silently
    # returns None and the live code falls back to defaults — i.e.
    # all 7 autonomy layers were partly inert in production until
    # this fix landed.
    signal_weights: str = "{}"        # Layer 2
    regime_overrides: str = "{}"      # Layer 3
    tod_overrides: str = "{}"         # Layer 4
    symbol_overrides: str = "{}"      # Layer 7
    prompt_layout: str = "{}"         # Layer 6

    # Layer 9 — capital allocator's recommended scale (1.0 baseline,
    # 0.5 = halved, 2.0 = doubled). Read by trade_pipeline:439.
    capital_scale: float = 1.0

    # Multi-Alpaca-account linkage. Read by multi_scheduler:877.
    alpaca_account_id: Optional[int] = None

    # Per-profile opt-in for AI model auto-tuning (off by default to
    # prevent surprise Sonnet/Opus calls under cost guard).
    ai_model_auto_tune: bool = False

    # Limit orders
    use_limit_orders: bool = False

    # Correlation management
    max_correlation: float = 0.7
    max_sector_positions: int = 5

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
