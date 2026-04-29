"""Human-readable display names for internal identifiers.

Internal code uses snake_case identifiers (strategy names, specialist
names, event types, crisis signal names). The UI should never show
those directly. This module is the single source of truth for the
mapping and is registered as a Jinja filter `| display_name` at app
startup so templates can use it uniformly.
"""

from __future__ import annotations


_DISPLAY_NAMES = {
    # Built-in strategies (multi-strategy registry)
    "market_engine":            "Market Structure Engine",
    "insider_cluster":          "Insider Buying Cluster",
    "earnings_drift":           "Earnings Drift",
    "vol_regime":               "Volatility Regime",
    "max_pain_pinning":         "Max Pain Pinning",
    "gap_reversal":             "Gap Reversal",
    # Expanded seed library
    "short_term_reversal":      "Short-Term Reversal",
    "sector_momentum_rotation": "Sector Momentum Rotation",
    "analyst_upgrade_drift":    "Analyst Revision Drift",
    "fifty_two_week_breakout":  "52-Week Breakout",
    "short_squeeze_setup":      "Short Squeeze Setup",
    "high_iv_rank_fade":        "High IV Rank Fade",
    "insider_selling_cluster":  "Insider Selling Cluster",
    "news_sentiment_spike":     "News Sentiment Spike",
    "volume_dryup_breakout":    "Volume Dry-up Breakout",
    "macd_cross_confirmation":  "MACD Cross with Confirmation",

    # Specialist AIs (ensemble)
    "earnings_analyst":     "Earnings Analyst",
    "pattern_recognizer":   "Pattern Recognizer",
    "sentiment_narrative":  "Sentiment & Narrative",
    "risk_assessor":        "Risk Assessor",

    # Event types (Phase 9)
    "sec_filing_detected":    "SEC Filing Detected",
    "earnings_imminent":      "Earnings Imminent",
    "price_shock":            "Price Shock",
    "prediction_big_winner":  "Big Winner Resolved",
    "prediction_big_loser":   "Big Loser Resolved",
    "strategy_deprecated":    "Strategy Deprecated",
    "crisis_state_change":    "Crisis State Change",

    # Crisis signals (Phase 10)
    "vix_elevated":            "VIX Elevated",
    "vix_crisis":              "VIX Crisis",
    "vix_severe":              "VIX Severe",
    "vix_inversion":           "VIX Term Inversion",
    "correlation_spike":       "Cross-Asset Correlation Spike",
    "bond_stock_divergence":   "Bond/Stock Divergence",
    "gold_rally":              "Gold Safe-Haven Rally",
    "credit_stress":           "Credit Spread Stress",
    "event_cluster":           "Price Shock Cluster",

    # Crisis reading field labels (shown as "Readings:" in dashboard)
    "vix":                     "VIX",
    "vix_5d_avg":              "VIX 5-day avg",
    "vix_term_ratio":          "VIX term ratio (3M/spot)",
    "cross_asset_corr":        "Cross-asset correlation",
    "tlt_5d_pct":              "TLT 5-day",
    "spy_5d_pct":              "SPY 5-day",
    "gld_5d_pct":              "GLD 5-day",
    "hyg_lqd_ratio_10d_pct":   "HYG/LQD 10-day",
    "price_shock_count_30m":   "Price shocks (30 min)",

    # Crisis levels
    "normal":     "Normal",
    "elevated":   "Elevated",
    "crisis":     "Crisis",
    "severe":     "Severe",

    # Auto-strategy lifecycle states
    "proposed":   "Proposed",
    "validated":  "Validated",
    "shadow":     "Shadow Trading",
    "active":     "Active",
    "retired":    "Retired",

    # AI cost-ledger `purpose` tags — what the call was for
    "single_analyze":          "Single-Symbol Analysis",
    "consensus_secondary":     "Consensus (Secondary Model)",
    "portfolio_review":        "Portfolio Review",
    "batch_select":            "Trade Selection (Batch)",
    "political_context":       "Political / Macro Context",
    "sec_diff":                "SEC Filing Diff",
    "strategy_proposal":       "Strategy Proposal (Auto-Gen)",
    "ensemble:earnings_analyst":     "Ensemble — Earnings Analyst",
    "ensemble:pattern_recognizer":   "Ensemble — Pattern Recognizer",
    "ensemble:sentiment_narrative":  "Ensemble — Sentiment & Narrative",
    "ensemble:risk_assessor":        "Ensemble — Risk Assessor",
    "uncategorized":           "Uncategorized",

    # Technical indicators / feature names (meta-model, predictions)
    "rsi":                     "RSI",
    "volume_ratio":            "Volume Ratio",
    "atr":                     "ATR",
    "adx":                     "ADX",
    "stoch_rsi":               "Stochastic RSI",
    "roc_10":                  "10-Day Rate of Change",
    "pct_from_52w_high":       "% From 52-Week High",
    "mfi":                     "Money Flow Index",
    "cmf":                     "Chaikin Money Flow",
    "squeeze":                 "Bollinger Squeeze",
    "pct_from_vwap":           "% From VWAP",
    "nearest_fib_dist":        "Fibonacci Distance",
    "gap_pct":                 "Gap %",
    "rel_strength_vs_sector":  "Relative Strength vs. Sector",
    "short_pct_float":         "Short % of Float",
    "put_call_ratio":          "Put/Call Ratio",
    "pe_trailing":             "P/E Ratio (Trailing)",
    "reddit_mentions":         "Reddit Mentions",
    "reddit_sentiment":        "Reddit Sentiment",
    "_market_signal_count":    "Market Signal Count",
    "score":                   "Strategy Score",
    "price":                   "Price",
    "confidence":              "AI Confidence",
    "signal":                  "Signal",
    "ensemble_confidence":     "Ensemble Confidence",
    "ensemble_verdict":        "Ensemble Verdict",
    "sector_trend":            "Sector Trend",
    "insider_direction":       "Insider Direction",
    "options_signal":          "Options Signal",
    "vwap_position":           "VWAP Position",
    "_regime":                 "Market Regime",

    # New alternative data features
    "congress_direction":       "Congressional Trading",
    "finra_short_vol_ratio":    "FINRA Short Volume Ratio",
    "insider_cluster":          "Insider Buying Cluster",
    "eps_revision_direction":   "EPS Revision Direction",
    "eps_revision_magnitude":   "EPS Revision %",

    # New macro features
    "_yield_spread_10y2y":      "10Y-2Y Yield Spread",
    "_curve_status":            "Yield Curve Status",
    "_cboe_skew":               "CBOE Skew Index",
    "_unemployment_rate":       "Unemployment Rate",
    "_cpi_yoy":                 "CPI Year-over-Year",

    # New crisis signals
    "skew_extreme":             "CBOE Skew Extreme",
    "yield_curve_inverted":     "Yield Curve Inverted",

    # New crisis readings
    "cboe_skew":                "CBOE Skew",
    "yield_spread_10y2y":       "10Y-2Y Spread",

    # Wave 2 signals
    "insider_near_earnings":    "Insider Activity Near Earnings",
    "_rotation_phase":          "Sector Rotation Phase",
    "dark_pool_pct":            "Dark Pool % of Volume",
    "earnings_surprise_streak": "Earnings Surprise Streak",
    "earnings_surprise_direction": "Earnings Surprise Direction",
    "_market_gex_regime":       "Market GEX Regime",

    # Exit trigger types
    "trailing_stop":            "Trailing Stop",
    "stop_loss":                "Stop Loss",
    "take_profit":              "Take Profit",
    "short_stop_loss":          "Short Stop Loss",
    "short_take_profit":        "Short Take Profit",
    "transcript_tone":          "Earnings Call Tone",
    "patent_velocity":          "Patent Filing Velocity",

    # Self-tuning parameter names — what self_tuning logs as the
    # `parameter_name` column in tuning_history. These are the
    # primary leak point for snake_case in the dashboard tuning
    # widget, the activity feed, and the weekly digest email.
    "ai_confidence_threshold":  "AI Confidence Threshold",
    "max_position_pct":         "Max Position Size (%)",
    "max_total_positions":      "Max Total Positions",
    "stop_loss_pct":            "Stop-Loss (%)",
    "take_profit_pct":          "Take-Profit (%)",
    "drawdown_pause_pct":       "Drawdown Pause Threshold",
    "drawdown_reduce_pct":      "Drawdown Reduce Threshold",
    "short_stop_loss_pct":      "Short Stop-Loss (%)",
    "short_take_profit_pct":    "Short Take-Profit (%)",
    "atr_multiplier_sl":        "ATR Stop Multiplier",
    "atr_multiplier_tp":        "ATR Target Multiplier",
    "trailing_atr_multiplier":  "Trailing Stop Multiplier",
    "max_correlation":          "Max Correlation",
    "max_sector_positions":     "Max Positions per Sector",
    "min_price":                "Min Stock Price",
    "max_price":                "Max Stock Price",
    "min_volume":               "Min Volume",
    "volume_surge_multiplier":  "Volume Surge Multiplier",
    "rsi_overbought":           "RSI Overbought Threshold",
    "rsi_oversold":             "RSI Oversold Threshold",
    "momentum_5d_gain":         "5-Day Momentum Gain (%)",
    "momentum_20d_gain":        "20-Day Momentum Gain (%)",
    "breakout_volume_threshold":"Breakout Volume Threshold",
    "gap_pct_threshold":        "Gap % Threshold",
    "avoid_earnings_days":      "Avoid Earnings (days)",
    "skip_first_minutes":       "Skip Opening Minutes",
    "use_atr_stops":            "ATR-Based Stops",
    "use_trailing_stops":       "Trailing Stops",
    "use_limit_orders":         "Limit Orders",
    "enable_short_selling":     "Short Selling",
    "enable_self_tuning":       "Self-Tuning",
    "enable_consensus":         "Multi-Model Consensus",
    "maga_mode":                "MAGA Mode",

    # LONG_SHORT_PLAN.md Phase 1+2 columns and meta-model features.
    "short_max_position_pct":   "Short Max Position",
    "short_max_hold_days":      "Short Max Hold Days",
    "target_short_pct":         "Target Short Share",
    "prediction_type":          "Prediction Type",

    # Strategy-toggle parameter names (self-tuner can disable a strategy
    # via these). Without explicit entries the fallback yields
    # "Strategy Gap And Go" — almost right but the conjunction "And"
    # reads weird; explicit entries fix it.
    "strategy_momentum_breakout": "Strategy: Momentum Breakout",
    "strategy_volume_spike":      "Strategy: Volume Spike",
    "strategy_mean_reversion":    "Strategy: Mean Reversion",
    "strategy_gap_and_go":        "Strategy: Gap & Go",

    # Bare strategy_type column values (no "strategy_" prefix) — used in
    # alpha-decay deprecation tables. Without these the fallback would
    # produce "Gap And Go" which reads weirdly.
    "momentum_breakout": "Momentum Breakout",
    "volume_spike":      "Volume Spike",
    "mean_reversion":    "Mean Reversion",
    "gap_and_go":        "Gap & Go",

    # Namespace prefixes for compound parameter keys like
    # `weight:insider_cluster`, `regime:volatile:stop_loss_pct`,
    # `tod:open:max_position_pct`, `symbol:NVDA:max_position_pct`,
    # `deprecate:insider_cluster`, `layout:alt_data`. The namespaced
    # display_name fallback recursively resolves each segment, so
    # explicit entries here make the prefix portion read naturally.
    "weight":          "Signal Intensity",
    "regime":          "Regime",
    "tod":             "Time of Day",
    "symbol":          "Symbol",
    "deprecate":       "Deprecate Strategy",
    "layout":          "Prompt Section",
    "self_commission": "Self-Commissioned Strategy",
    "capital_scale":   "Capital Scale",
    # NOTE: Don't add bare entries for section names like `alt_data` /
    # `political_context` here — `political_context` is also an AI-cost
    # purpose label with a different (existing) display ("Political /
    # Macro Context") and overriding it breaks AI cost rendering.
    # The `layout:alt_data` fallback renders as "Prompt Section —
    # Alt Data" which is perfectly readable.
}


# ---------------------------------------------------------------------------
# Value formatting
# ---------------------------------------------------------------------------

# Parameters whose stored value is a fractional decimal (0.07 = 7%) and
# should be displayed as a percentage in any user-facing context.
_PERCENTAGE_PARAMS = frozenset({
    "max_position_pct",
    "stop_loss_pct",
    "take_profit_pct",
    "drawdown_pause_pct",
    "drawdown_reduce_pct",
    "short_stop_loss_pct",
    "short_take_profit_pct",
    "max_correlation",
    "gap_pct_threshold",
    # NB: rsi_overbought / rsi_oversold / ai_confidence_threshold are
    # already in 0-100 range — NOT fractional decimals — so they're
    # NOT in this set.
})

# Boolean toggles — display as Enabled / Disabled rather than 0/1.
_BOOLEAN_PARAMS = frozenset({
    "enable_short_selling", "enable_self_tuning", "enable_consensus",
    "use_atr_stops", "use_trailing_stops", "use_limit_orders",
    "maga_mode",
    "strategy_momentum_breakout", "strategy_volume_spike",
    "strategy_mean_reversion", "strategy_gap_and_go",
})


def format_param_value(name: str, value) -> str:
    """Render a tuning-parameter value for human display.

    `name` is the snake_case parameter key. `value` may be a string,
    int, or float (sqlite stores as TEXT). Returns a string in the
    most natural form for the parameter type.

    Examples:
      format_param_value("max_position_pct", 0.07)    → "7.0%"
      format_param_value("max_position_pct", 0.0805)  → "8.05%"
      format_param_value("ai_confidence_threshold", 60) → "60"
      format_param_value("enable_short_selling", 1)   → "Enabled"
      format_param_value("rsi_oversold", 25.0)        → "25"
    """
    if value is None or value == "":
        return ""
    # Coerce string-stored numeric values
    raw = value
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)

    if name in _BOOLEAN_PARAMS:
        return "Enabled" if v >= 0.5 else "Disabled"

    if name in _PERCENTAGE_PARAMS:
        # 0.07 → "7.0%"; 0.0805 → "8.05%". One decimal when exact at
        # that precision, two otherwise. Avoids "7%" looking like an int.
        pct = v * 100
        if abs(pct - round(pct, 1)) < 1e-9:
            return f"{pct:.1f}%"
        return f"{pct:.2f}%"

    # Integer-valued params display as int (avoid "60.0" for AI conf etc.)
    if v == int(v):
        return str(int(v))

    # Generic float — 2 decimals
    return f"{v:.2f}"


def _is_ticker_like(s: str) -> bool:
    """A bare uppercase token is probably a stock ticker (NVDA, AAPL).
    Don't title-case it — preserve as-is so the user sees the actual
    symbol they know."""
    return s.isupper() and 1 <= len(s) <= 6 and s.isalpha()


def display_name(internal: str) -> str:
    """Return the human-readable label for an internal identifier.

    Unknown identifiers fall back to title-casing with underscores
    replaced by spaces — so a new auto-generated strategy like
    `auto_oversold_vol_confirm` becomes `Auto Oversold Vol Confirm`
    without any code change required here.
    """
    if not isinstance(internal, str) or not internal:
        return str(internal) if internal is not None else ""
    if internal in _DISPLAY_NAMES:
        return _DISPLAY_NAMES[internal]
    # Preserve ticker-shaped tokens (NVDA, AAPL) verbatim
    if _is_ticker_like(internal):
        return internal
    # Fallback: pretty-print snake_case (and namespaced "x:y" keys like
    # "ensemble:earnings_analyst" → "Ensemble — Earnings Analyst")
    if ":" in internal:
        head, _, tail = internal.partition(":")
        return f"{display_name(head)} — {display_name(tail)}"
    return " ".join(w.capitalize() for w in internal.replace("-", "_").split("_") if w)


# ---------------------------------------------------------------------------
# Reading-value formatter: render raw metric values with the right units
# ---------------------------------------------------------------------------

# Per-field unit hints used by `format_reading_value`. Keys must match the
# raw metric field names (same as in `_DISPLAY_NAMES` above).
_READING_UNITS = {
    "vix":                   "number",
    "vix_5d_avg":            "number",
    "vix_term_ratio":        "ratio",
    "cross_asset_corr":      "ratio",
    "tlt_5d_pct":            "pct",
    "spy_5d_pct":            "pct",
    "gld_5d_pct":            "pct",
    "hyg_lqd_ratio_10d_pct": "pct",
    "price_shock_count_30m": "count",
}


def format_reading_value(field: str, value) -> str:
    """Format a raw reading value with units appropriate to the field.

    Unknown fields get a safe str() fallback. Used to turn internal
    snake_case metric outputs like `tlt_5d_pct=0.66` into user-readable
    `+0.66%`.
    """
    if value is None:
        return "—"
    unit = _READING_UNITS.get(field, "number")
    try:
        if unit == "pct":
            return f"{float(value):+.2f}%"
        if unit == "ratio":
            return f"{float(value):.3f}"
        if unit == "count":
            return f"{int(value)}"
        # number (default)
        v = float(value)
        # VIX-style: 2 decimals if small, 1 decimal if >= 10
        return f"{v:.1f}" if abs(v) >= 10 else f"{v:.2f}"
    except (TypeError, ValueError):
        return str(value)


def friendly_time(iso_str: str) -> str:
    """Convert a UTC ISO timestamp to human-readable US/Eastern time.

    All trade timestamps are stored as UTC. The US equity market
    operates on Eastern time, so we convert and label accordingly.

    Examples:
        "2026-04-15T19:42:12.433431" → "Apr 15, 3:42 PM ET"
        "2026-04-14T13:30:00"        → "Apr 14, 9:30 AM ET"
        None or ""                   → "--"
    """
    if not iso_str:
        return "--"
    try:
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo
        clean = iso_str.replace("Z", "").split("+")[0]
        if "." in clean:
            dt = datetime.strptime(clean, "%Y-%m-%dT%H:%M:%S.%f")
        elif "T" in clean:
            dt = datetime.strptime(clean[:19], "%Y-%m-%dT%H:%M:%S")
        elif " " in clean and len(clean) >= 19:
            dt = datetime.strptime(clean[:19], "%Y-%m-%d %H:%M:%S")
        else:
            dt = datetime.strptime(clean[:10], "%Y-%m-%d")
            return dt.strftime("%b %-d")
        dt_utc = dt.replace(tzinfo=timezone.utc)
        dt_et = dt_utc.astimezone(ZoneInfo("America/New_York"))
        return dt_et.strftime("%b %-d, %-I:%M %p ET")
    except Exception:
        return iso_str[:16] if len(iso_str) > 16 else iso_str


def friendly_date(iso_str: str) -> str:
    """Convert an ISO date or timestamp to "Mon DD, YYYY".

    Accepts either bare dates ("2026-03-28") or full timestamps —
    the time portion is dropped. Useful when only the calendar date
    is meaningful but you still want the year visible (e.g., user
    creation dates that may be months/years old).

    Examples:
        "2026-03-28"           → "Mar 28, 2026"
        "2026-04-23T14:36:00"  → "Apr 23, 2026"
        None or ""             → "--"
    """
    if not iso_str:
        return "--"
    try:
        from datetime import datetime
        clean = iso_str.replace("Z", "").split("+")[0][:10]
        dt = datetime.strptime(clean, "%Y-%m-%d")
        return dt.strftime("%b %-d, %Y")
    except Exception:
        return iso_str[:10] if len(iso_str) >= 10 else iso_str


def register(app) -> None:
    """Wire up the `display_name`, `reading_value`, `friendly_time`,
    and `friendly_date` Jinja filters."""
    app.jinja_env.filters["display_name"] = display_name
    app.jinja_env.filters["reading_value"] = format_reading_value
    app.jinja_env.filters["friendly_time"] = friendly_time
    app.jinja_env.filters["friendly_date"] = friendly_date
