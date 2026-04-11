# QuantOpsAI — Smart Execution Plan

## Status Tracker

| # | Feature | Status |
|---|---|---|
| 1 | ATR-Based Stops | DONE |
| 2 | Trailing Stops | DONE |
| 3 | Limit Orders | DONE |
| 4 | Correlation Management | DONE |
| 5 | Cleaner AI Prompts | DONE |
| 6 | Backtesting | DONE |

---

## Feature 1: ATR-Based Stops

**Problem:** Fixed 6% stop applies equally to a stock moving 1%/day and one moving 8%/day.

**Solution:** Calculate each stock's 14-day ATR (Average True Range). Set stop-loss at 2x ATR below entry. Set take-profit at 3x ATR above entry. This adapts to each stock's actual volatility.

**Example:**
- AAPL: ATR = $3.50, price = $250. Stop = $243 (1.4%), TP = $260.50 (4.2%)
- SNDL: ATR = $0.15, price = $1.50. Stop = $1.20 (20%), TP = $1.95 (30%)

The system already calculates ATR-related indicators in add_indicators(). We just need to use them.

**Files:**
- `portfolio_manager.py` — add `calculate_atr_stops(symbol, entry_price, atr_multiplier_sl=2.0, atr_multiplier_tp=3.0)` that fetches bars, calculates ATR, returns stop/take-profit prices
- `aggressive_trader.py` — when executing a trade, call calculate_atr_stops to get the actual stop/TP prices instead of using fixed percentages. Store the actual prices on the trade.
- `trader.py` — check_exits compares current price against stored stop/TP prices instead of calculating percentage from entry
- `user_context.py` — add `use_atr_stops: bool = True`, `atr_multiplier_sl: float = 2.0`, `atr_multiplier_tp: float = 3.0`
- `models.py` — add columns to trading_profiles
- `journal.py` — ensure stop_loss and take_profit on trades table store actual PRICES not percentages
- `templates/settings.html` — add ATR toggle and multiplier sliders
- `views.py` — save new fields

---

## Feature 2: Trailing Stops

**Problem:** A trade up 4% has zero protection. If it reverses, hits -6% stop = 10% swing loss.

**Solution:** Once a position moves in your favor, trail the stop behind it. Implementation uses a high-water mark per position.

**Logic:**
- Track highest price since entry for longs, lowest price since entry for shorts
- Trailing stop = high_water - (ATR * trailing_multiplier)
- When current price drops below trailing stop → exit
- Never move the trailing stop backwards (only tightens)

**Files:**
- Create `trailing_stops.py`:
  - `update_trailing_stops(positions, ctx)` — for each position, fetch recent bars, find high since entry, calculate trailing stop level, return list of triggered exits
  - Uses a simple file/DB store for high-water marks per symbol per profile
- `models.py` — add `position_tracking` table: profile_id, symbol, entry_price, entry_date, high_water, low_water, trailing_stop_price
- `trader.py` — in check_exits, call trailing stop check alongside fixed stop check
- `aggressive_trader.py` — on trade execution, create position_tracking record
- `user_context.py` — add `use_trailing_stops: bool = True`, `trailing_atr_multiplier: float = 1.5`
- `templates/settings.html` — trailing stop toggle and ATR multiplier slider

---

## Feature 3: Limit Orders

**Problem:** Market orders fill at whatever price is available. Slippage on volatile small caps.

**Solution:** Use limit orders at current price for entries. If not filled within 5 minutes, cancel.

**Files:**
- `aggressive_trader.py` — change `submit_order(type="market")` to `submit_order(type="limit", limit_price=current_price)` for BUY/SHORT entries. Keep exits as market orders (need guaranteed fill on stop-loss).
- Add a background task in scheduler to check for unfilled limit orders and cancel after timeout
- `user_context.py` — add `use_limit_orders: bool = True`, `limit_order_timeout_min: int = 5`
- `multi_scheduler.py` — add `_task_check_pending_orders(ctx)` that runs every cycle, cancels stale limit orders

---

## Feature 4: Correlation Management

**Problem:** 5 crypto positions all correlated to BTC. One bad day kills everything.

**Solution:** Before opening a new position, check correlation with existing positions. Limit exposure to correlated groups.

**Files:**
- Create `correlation.py`:
  - `check_correlation(symbol, existing_positions, ctx)` — fetch 20-day returns for the new symbol and all existing positions, calculate pairwise correlation. If the new symbol has >0.7 correlation with any existing position, flag it.
  - `get_sector_exposure(positions)` — rough sector classification (crypto, tech, energy, etc.) based on which universe the symbol belongs to
- `aggressive_trader.py` — before executing, call check_correlation. If too correlated, reduce position size by 50% or skip.
- `user_context.py` — add `max_correlation: float = 0.7`, `max_sector_positions: int = 3`

---

## Feature 5: Cleaner AI Prompts

**Problem:** Prompt is bloated with context from 8 different sources. Haiku can't reason well through noise.

**Solution:** Prioritize and compress. Most important info first, concise format, cut redundancy.

**Files:**
- `ai_analyst.py` — restructure the prompt:
  1. Technical data (keep, it's the core)
  2. ONE LINE market regime: "BEAR market, VIX 25, favor shorts"
  3. ONE LINE stock history: "Your record on SOFI: 2W/6L, avoid"
  4. ONE LINE self-tuning: "Your overall win rate: 45%. Higher confidence predictions perform better."
  5. Political context only if MAGA mode AND volatility is HIGH (don't inject when markets are calm)
  6. Earnings warning only if within 5 days
  7. Remove: time-of-day patterns (marginal value), cross-profile verbose text, full lessons learned history
- `self_tuning.py` — add `build_concise_context(ctx, symbol)` that returns 3-4 lines max instead of the current wall of text

---

## Feature 6: Backtesting

**Problem:** 5 new strategy engines with zero historical validation.

**Solution:** Run each strategy against 6 months of historical data. Measure win rate, P&L, max drawdown, Sharpe ratio.

**Files:**
- Update `backtester.py` — currently exists but only works with the old combined strategy. Update to accept any strategy function via the router.
  - `backtest_strategy(market_type, days=180)` — fetch historical bars for the segment's universe, run the strategy engine day by day, simulate entries/exits with ATR-based stops
  - Return: total_return, win_rate, max_drawdown, sharpe_ratio, num_trades, avg_hold_days
- `main.py` — add CLI command `python main.py backtest-strategy midcap 180`
- Create a report that shows per-strategy-engine results side by side

---

## Build Order

1. **ATR-Based Stops** — most impactful, fixes the core stop-loss problem
2. **Trailing Stops** — protects gains, prevents winners becoming losers
3. **Cleaner AI Prompts** — better AI decisions immediately
4. **Limit Orders** — better entry prices
5. **Correlation Management** — portfolio risk reduction
6. **Backtesting** — validate everything works historically
