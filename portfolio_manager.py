"""Portfolio-level risk management and position sizing."""

import logging
import sqlite3

import config
from market_data import get_bars

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ATR-Based Stops (Feature 1)
# ---------------------------------------------------------------------------

def calculate_atr_stops(symbol, entry_price, atr_multiplier_sl=2.0, atr_multiplier_tp=3.0):
    """Calculate stop-loss and take-profit prices based on ATR.

    Fetches 30 bars for *symbol*, computes a simple 14-day ATR (average of
    high - low), then returns dollar-price levels for stop and take-profit.

    Returns (stop_price, take_profit_price, atr_value) or (None, None, None)
    on failure.
    """
    try:
        bars = get_bars(symbol, limit=30)
        if bars is None or bars.empty or len(bars) < 14:
            logger.warning("calculate_atr_stops: not enough bars for %s (%d)",
                           symbol, len(bars) if bars is not None else 0)
            return None, None, None

        # Simple ATR: average of (high - low) over the last 14 bars
        ranges = (bars["high"] - bars["low"]).tail(14)
        atr = float(ranges.mean())

        if atr <= 0:
            return None, None, None

        stop_price = entry_price - (atr * atr_multiplier_sl)
        tp_price = entry_price + (atr * atr_multiplier_tp)

        # Ensure stop is positive
        if stop_price <= 0:
            stop_price = entry_price * 0.01  # minimal floor

        return round(stop_price, 4), round(tp_price, 4), round(atr, 4)

    except Exception as exc:
        logger.warning("calculate_atr_stops failed for %s: %s", symbol, exc)
        return None, None, None


# ---------------------------------------------------------------------------
# Trailing Stops (Feature 2)
# ---------------------------------------------------------------------------

def check_trailing_stops(positions, ctx=None):
    """Check positions for trailing stop triggers.

    For each profitable long position, tracks the highest recent price and
    sets a trailing stop at high_water - (ATR * trailing_multiplier).
    For profitable short positions, tracks the lowest recent price.

    Only trails *profitable* positions — losers are handled by the regular stop.

    Returns list of triggered exit signals (same format as
    check_stop_loss_take_profit).
    """
    if ctx is None:
        return []

    trailing_multiplier = getattr(ctx, "trailing_atr_multiplier", 1.5)
    triggered = []

    for pos in positions:
        symbol = pos.get("symbol")
        current_price = float(pos.get("current_price", 0))
        entry_price = float(pos.get("avg_entry_price", 0))
        qty = pos.get("qty", 0)

        if entry_price <= 0 or current_price <= 0:
            continue

        is_short = int(qty) < 0

        # Only trail profitable positions
        if is_short:
            # Profitable short: current price < entry price
            if current_price >= entry_price:
                continue
        else:
            # Profitable long: current price > entry price
            if current_price <= entry_price:
                continue

        try:
            bars = get_bars(symbol, limit=30)
            if bars is None or bars.empty or len(bars) < 14:
                continue

            # Compute ATR from last 14 bars
            ranges = (bars["high"] - bars["low"]).tail(14)
            atr = float(ranges.mean())
            if atr <= 0:
                continue

            recent_bars = bars.tail(5)

            if is_short:
                # Track the lowest price (best for short), trail upward
                low_water = float(recent_bars["low"].min())
                trailing_stop = low_water + (atr * trailing_multiplier)
                if current_price > trailing_stop:
                    abs_qty = abs(int(qty))
                    pct_from_entry = (current_price - entry_price) / entry_price
                    triggered.append({
                        "symbol": symbol,
                        "signal": "SELL",
                        "reason": (
                            f"Trailing stop triggered (short): price ${current_price:.2f} "
                            f"> trailing stop ${trailing_stop:.2f} "
                            f"(low water ${low_water:.2f} + {trailing_multiplier:.1f}x ATR ${atr:.2f})"
                        ),
                        "price": current_price,
                        "qty": abs_qty,
                        "trigger": "trailing_stop",
                        "is_short": True,
                    })
            else:
                # Track the highest price, trail downward
                high_water = float(recent_bars["high"].max())
                trailing_stop = high_water - (atr * trailing_multiplier)
                if current_price < trailing_stop:
                    triggered.append({
                        "symbol": symbol,
                        "signal": "SELL",
                        "reason": (
                            f"Trailing stop triggered: price ${current_price:.2f} "
                            f"< trailing stop ${trailing_stop:.2f} "
                            f"(high water ${high_water:.2f} - {trailing_multiplier:.1f}x ATR ${atr:.2f})"
                        ),
                        "price": current_price,
                        "qty": int(qty),
                        "trigger": "trailing_stop",
                    })

        except Exception as exc:
            logger.warning("Trailing stop check failed for %s: %s", symbol, exc)
            continue

    return triggered


def calculate_position_size(symbol, signal, account_info, current_positions,
                            max_position_pct=None):
    """Calculate the number of shares to trade based on signal strength and risk limits.

    Args:
        symbol: Ticker string.
        signal: Strategy signal dict with keys 'signal', 'price', and optionally
                'ai_confidence' (0-1).
        account_info: Dict with at least 'equity' and 'cash'.
        current_positions: Dict mapping symbol -> position dict (with 'market_value').
        max_position_pct: Max fraction of equity for one position.  Falls back
                          to config.MAX_POSITION_PCT when None.

    Returns:
        dict with 'qty', 'dollars', 'reason'.
    """
    if max_position_pct is None:
        max_position_pct = config.MAX_POSITION_PCT

    equity = account_info.get("equity", 0)
    cash = account_info.get("cash", 0)
    price = signal.get("price", 0)
    action = signal.get("signal", "HOLD")
    ai_confidence = signal.get("ai_confidence")

    if price <= 0:
        return {"qty": 0, "dollars": 0, "reason": "Invalid price"}

    # Base allocation as fraction of max position
    max_dollars = equity * max_position_pct

    signal_multipliers = {
        "STRONG_BUY": 1.0,
        "BUY": 0.75,
        "WEAK_BUY": 0.5,
    }

    multiplier = signal_multipliers.get(action, 0)
    if multiplier == 0:
        return {"qty": 0, "dollars": 0, "reason": f"Signal '{action}' is not a buy signal"}

    # Scale by AI confidence if available
    if ai_confidence is not None and 0 < ai_confidence <= 1:
        multiplier *= ai_confidence

    dollars = max_dollars * multiplier

    # Don't exceed available cash
    dollars = min(dollars, cash)

    qty = int(dollars / price)
    actual_dollars = qty * price

    if qty <= 0:
        return {"qty": 0, "dollars": 0, "reason": "Position size too small for price"}

    return {
        "qty": qty,
        "dollars": actual_dollars,
        "reason": f"{action} -> {multiplier:.0%} of max position (${max_dollars:,.0f})",
    }


def check_portfolio_constraints(symbol, proposed_trade, current_positions, account_info,
                                max_position_pct=None, max_total_positions=None):
    """Check whether a proposed trade is allowed under portfolio risk rules.

    Args:
        symbol: Ticker string.
        proposed_trade: Dict with 'side', 'qty', 'price'.
        current_positions: Dict mapping symbol -> position dict.
        account_info: Dict with 'equity', 'cash'.
        max_position_pct: Max fraction of equity for one position.  Falls back
                          to config.MAX_POSITION_PCT when None.
        max_total_positions: Max number of simultaneous positions.  Falls back
                             to config.MAX_TOTAL_POSITIONS when None.

    Returns:
        (allowed: bool, reason: str)
    """
    if max_position_pct is None:
        max_position_pct = config.MAX_POSITION_PCT
    if max_total_positions is None:
        max_total_positions = config.MAX_TOTAL_POSITIONS

    side = proposed_trade.get("side", "").lower()
    qty = proposed_trade.get("qty", 0)
    price = proposed_trade.get("price", 0)
    equity = account_info.get("equity", 0)
    cash = account_info.get("cash", 0)

    # Sells are always allowed (reduces risk)
    if side == "sell":
        return True, "Sell orders are always permitted"

    if qty <= 0:
        return False, "Quantity must be positive"

    # Check max total positions
    num_current = len(current_positions)
    if symbol not in current_positions and num_current >= max_total_positions:
        return False, (
            f"Already at max positions ({max_total_positions}). "
            f"Close a position before opening a new one."
        )

    # Check single-position concentration
    trade_value = qty * price
    if equity > 0 and trade_value / equity > max_position_pct:
        max_allowed = equity * max_position_pct
        return False, (
            f"Trade value ${trade_value:,.2f} exceeds {max_position_pct:.0%} "
            f"of equity (max ${max_allowed:,.2f})"
        )

    # Check if adding to an existing position would breach the limit
    if symbol in current_positions:
        existing_value = abs(float(current_positions[symbol].get("market_value", 0)))
        combined = existing_value + trade_value
        if equity > 0 and combined / equity > max_position_pct:
            return False, (
                f"Combined position ${combined:,.2f} would exceed "
                f"{max_position_pct:.0%} of equity"
            )

    # Check sufficient cash
    if trade_value > cash:
        return False, (
            f"Insufficient cash: need ${trade_value:,.2f}, have ${cash:,.2f}"
        )

    return True, "Trade passes all portfolio constraints"


def check_stop_loss_take_profit(positions, stop_loss_pct=None, take_profit_pct=None,
                                short_stop_loss_pct=None, short_take_profit_pct=None):
    """Check all open positions against stop-loss and take-profit thresholds.

    Args:
        positions: List of position dicts, each with at least:
            'symbol', 'current_price', 'avg_entry_price', 'qty',
            and optionally 'stop_loss', 'take_profit'.
        stop_loss_pct: Default stop-loss percentage for long positions.  Falls
                       back to config.DEFAULT_STOP_LOSS_PCT when None.
        take_profit_pct: Default take-profit percentage for long positions.
                         Falls back to config.DEFAULT_TAKE_PROFIT_PCT when None.
        short_stop_loss_pct: Stop-loss percentage for short positions.  Falls
                             back to stop_loss_pct when None.
        short_take_profit_pct: Take-profit percentage for short positions.
                               Falls back to take_profit_pct when None.

    Returns:
        List of sell signal dicts for positions that have triggered, each with:
        'symbol', 'signal', 'reason', 'price', 'qty', 'trigger'.
    """
    if stop_loss_pct is None:
        stop_loss_pct = config.DEFAULT_STOP_LOSS_PCT
    if take_profit_pct is None:
        take_profit_pct = config.DEFAULT_TAKE_PROFIT_PCT
    if short_stop_loss_pct is None:
        short_stop_loss_pct = stop_loss_pct
    if short_take_profit_pct is None:
        short_take_profit_pct = take_profit_pct

    triggered = []

    for pos in positions:
        symbol = pos.get("symbol")
        current_price = float(pos.get("current_price", 0))
        entry_price = float(pos.get("avg_entry_price", 0))
        qty = pos.get("qty", 0)

        if entry_price <= 0 or current_price <= 0:
            continue

        pct_change = (current_price - entry_price) / entry_price

        # Use per-position thresholds if set, otherwise use provided defaults
        pos_stop_loss = pos.get("stop_loss") or stop_loss_pct
        pos_take_profit = pos.get("take_profit") or take_profit_pct

        # Detect short positions by negative qty
        is_short = int(qty) < 0

        if is_short:
            abs_qty = abs(int(qty))
            # For shorts, use short-specific thresholds (wider stops)
            pos_short_sl = pos.get("stop_loss") or short_stop_loss_pct
            pos_short_tp = pos.get("take_profit") or short_take_profit_pct
            # For shorts: price going UP is bad (stop-loss), price going DOWN is good (take-profit)
            if pct_change >= pos_short_sl:
                triggered.append({
                    "symbol": symbol,
                    "signal": "SELL",
                    "reason": (
                        f"Short stop-loss triggered: price up {pct_change:+.2%} "
                        f"(threshold +{pos_short_sl:.0%})"
                    ),
                    "price": current_price,
                    "qty": abs_qty,
                    "trigger": "short_stop_loss",
                    "is_short": True,
                })
            elif pct_change <= -pos_short_tp:
                triggered.append({
                    "symbol": symbol,
                    "signal": "SELL",
                    "reason": (
                        f"Short take-profit triggered: price down {pct_change:+.2%} "
                        f"(threshold -{pos_short_tp:.0%})"
                    ),
                    "price": current_price,
                    "qty": abs_qty,
                    "trigger": "short_take_profit",
                    "is_short": True,
                })
        else:
            if pct_change <= -pos_stop_loss:
                triggered.append({
                    "symbol": symbol,
                    "signal": "SELL",
                    "reason": (
                        f"Stop-loss triggered: {pct_change:+.2%} "
                        f"(threshold -{pos_stop_loss:.0%})"
                    ),
                    "price": current_price,
                    "qty": qty,
                    "trigger": "stop_loss",
                })
            elif pct_change >= pos_take_profit:
                triggered.append({
                    "symbol": symbol,
                    "signal": "SELL",
                    "reason": (
                        f"Take-profit triggered: {pct_change:+.2%} "
                        f"(threshold +{pos_take_profit:.0%})"
                    ),
                    "price": current_price,
                    "qty": qty,
                    "trigger": "take_profit",
                })

    return triggered


def get_risk_summary(account_info, positions, max_total_positions=None,
                     max_position_pct=None):
    """Compute portfolio-level risk metrics.

    Args:
        account_info: Dict with 'equity', 'cash'.
        positions: List of position dicts with 'symbol', 'market_value',
                   'unrealized_pl', 'qty', 'current_price', 'avg_entry_price'.
        max_total_positions: Max simultaneous positions.  Falls back to
                             config.MAX_TOTAL_POSITIONS when None.
        max_position_pct: Max fraction of equity for one position.  Falls back
                          to config.MAX_POSITION_PCT when None.

    Returns:
        Dict with concentration and risk figures.
    """
    if max_total_positions is None:
        max_total_positions = config.MAX_TOTAL_POSITIONS
    if max_position_pct is None:
        max_position_pct = config.MAX_POSITION_PCT

    equity = account_info.get("equity", 0)
    cash = account_info.get("cash", 0)
    num_positions = len(positions)

    total_invested = sum(abs(float(p.get("market_value", 0))) for p in positions)
    total_unrealized_pnl = sum(float(p.get("unrealized_pl", 0)) for p in positions)

    cash_pct = (cash / equity * 100) if equity > 0 else 0
    invested_pct = (total_invested / equity * 100) if equity > 0 else 0

    # Per-position breakdown
    position_weights = {}
    largest_position = {"symbol": None, "weight": 0}
    for p in positions:
        symbol = p.get("symbol")
        mv = abs(float(p.get("market_value", 0)))
        weight = (mv / equity * 100) if equity > 0 else 0
        position_weights[symbol] = weight
        if weight > largest_position["weight"]:
            largest_position = {"symbol": symbol, "weight": weight}

    # Slots remaining
    available_slots = max(0, max_total_positions - num_positions)

    return {
        "equity": equity,
        "cash": cash,
        "cash_pct": round(cash_pct, 2),
        "total_invested": round(total_invested, 2),
        "invested_pct": round(invested_pct, 2),
        "num_positions": num_positions,
        "max_positions": max_total_positions,
        "available_slots": available_slots,
        "total_unrealized_pnl": round(total_unrealized_pnl, 2),
        "position_weights": {k: round(v, 2) for k, v in position_weights.items()},
        "largest_position": largest_position,
        "max_position_pct": max_position_pct * 100,
    }


def check_drawdown(ctx, account_info, db_path=None):
    """Check current drawdown from peak equity.

    Returns:
        dict with keys: drawdown_pct, peak_equity, current_equity, action
        action is one of: "normal", "reduce", "pause"
    """
    current_equity = account_info.get("equity", 0)
    db = db_path or (ctx.db_path if ctx else None)

    peak_equity = current_equity  # default if no snapshots

    if db:
        try:
            conn = sqlite3.connect(db)
            row = conn.execute(
                "SELECT MAX(equity) FROM daily_snapshots"
            ).fetchone()
            conn.close()
            if row and row[0] is not None:
                peak_equity = max(float(row[0]), current_equity)
        except Exception as exc:
            logger.debug("Could not query daily_snapshots for peak equity: %s", exc)

    if peak_equity <= 0:
        return {
            "drawdown_pct": 0.0,
            "peak_equity": peak_equity,
            "current_equity": current_equity,
            "action": "normal",
        }

    drawdown_pct = (peak_equity - current_equity) / peak_equity * 100

    pause_threshold = (ctx.drawdown_pause_pct * 100) if ctx else 20.0
    reduce_threshold = (ctx.drawdown_reduce_pct * 100) if ctx else 10.0

    if drawdown_pct >= pause_threshold:
        action = "pause"
    elif drawdown_pct >= reduce_threshold:
        action = "reduce"
    else:
        action = "normal"

    return {
        "drawdown_pct": round(drawdown_pct, 2),
        "peak_equity": peak_equity,
        "current_equity": current_equity,
        "action": action,
    }
