# QuantOpsAI — Per-Market-Type Strategy Engines

## Status Tracker

| # | Market Type | Strategy Engine | Status |
|---|---|---|---|
| 1 | Micro Cap ($1-$5) | `strategy_micro.py` | DONE |
| 2 | Small Cap ($5-$20) | `strategy_small.py` | DONE |
| 3 | Mid Cap ($20-$100) | `strategy_mid.py` | DONE |
| 4 | Large Cap ($50-$500) | `strategy_large.py` | DONE |
| 5 | Crypto | `strategy_crypto.py` | DONE |
| 6 | Strategy Router | `strategy_router.py` + `aggressive_trader.py` | DONE |
| 7 | Segment Definitions | Update `segments.py` | DONE |
| 8 | Settings UI | Split market types | DONE |

---

## Problem

One strategy (`aggressive_combined_strategy()`) runs for all market types. It uses the same RSI thresholds, volume multipliers, and pattern detection for a $0.50 penny stock, a $50 mid-cap, and Bitcoin. This is wrong:

- Crypto: 6,248 predictions, ZERO trades (strategies always return SELL/HOLD)
- MicroSmall lumps $1 stocks with $15 stocks (completely different behavior)
- Mid Cap has 64% prediction win rate but still loses money on trades
- No strategy exists for large caps

---

## Architecture

### Current Flow
```
Every candidate → aggressive_combined_strategy() → 4 fixed strategies → score → AI review
```

### New Flow
```
Every candidate → strategy_router(symbol, market_type, ctx) → market-specific strategy → AI review
```

The router picks the right strategy engine based on the profile's market_type. Each engine has its own strategies tuned for that market's behavior.

---

## Strategy Engine 1: Micro Cap ($1-$5)

**File:** `strategy_micro.py`

**Market characteristics:** Extreme volatility, low liquidity, penny-stock behavior, catalyst-driven, can 2x or lose 50% in a day. Many are garbage — need strong filters.

**Strategies:**

1. **Volume Explosion** — The #1 signal for micro-caps. When volume is 5x+ average AND price is up, something is happening (news, catalyst, pump). Enter early.
   - BUY: volume > 5x 20-day avg AND price up > 5% on the day AND RSI < 75
   - EXIT: volume drops below 2x avg (catalyst fading)

2. **Penny Reversal** — Deep oversold bounces. Micro-caps can drop 30-40% then bounce 20%.
   - BUY: RSI < 20 AND price > 20% below 10-day SMA AND volume increasing
   - EXIT: price returns to 10-day SMA OR RSI > 50

3. **Breakout Above Resistance** — Price breaks a key level with volume.
   - BUY: price > 10-day high AND volume > 3x avg
   - EXIT: price drops below breakout level (failed breakout)

4. **Avoid Traps** — Filter OUT stocks that look good technically but are death traps.
   - SKIP if: average volume < 100K (too illiquid), price declining for 10+ consecutive days (falling knife), spread > 5% (market maker trap)

**Default parameters:**
- stop_loss: 10% (micro-caps need huge room)
- take_profit: 15% (they move fast when they move)
- max_position: 5% of equity (small bets — these are risky)
- min_volume: 100,000
- volume_surge_threshold: 5x

---

## Strategy Engine 2: Small Cap ($5-$20)

**File:** `strategy_small.py`

**Market characteristics:** Volatile but more established than micro-caps. Sector-sensitive. Can have real earnings and fundamentals. Mean reversion works better here.

**Strategies:**

1. **Mean Reversion** — Classic oversold bounce. Works well for established small caps.
   - BUY: RSI < 28 AND price > 8% below 20-day SMA
   - EXIT: price returns to 20-day SMA OR RSI > 55

2. **Volume Spike Entry** — Institutional interest or catalyst.
   - BUY: volume > 3x 20-day avg AND price up > 3% AND RSI 30-65
   - EXIT: 2 consecutive red days with declining volume

3. **Gap and Go** — Opening gaps with momentum follow-through.
   - BUY: open > 2.5% above previous close AND volume > 1.5x avg
   - EXIT: price drops below today's open (gap fill)

4. **Momentum Continuation** — Riding established uptrends.
   - BUY: price above 20-day SMA AND SMA20 slope positive AND RSI 50-70 AND volume > avg
   - EXIT: price closes below 20-day SMA

**Default parameters:**
- stop_loss: 6%
- take_profit: 8%
- max_position: 8% of equity
- min_volume: 300,000
- volume_surge_threshold: 3x

---

## Strategy Engine 3: Mid Cap ($20-$100)

**File:** `strategy_mid.py`

**Market characteristics:** Institutional ownership, follows sectors and indices. More liquid. Momentum strategies work well — moves are more sustained and predictable.

**Strategies:**

1. **Sector Momentum** — Mid-caps move with their sector. If tech sector is strong, tech mid-caps follow.
   - BUY: stock RSI > 50 AND sector ETF (XLK, XLF, etc.) trending up AND volume > avg
   - EXIT: stock drops below 20-day SMA OR sector reverses

2. **Breakout with Volume** — Clean breakouts above resistance levels.
   - BUY: price > 20-day high AND volume > 2x avg AND RSI 55-75
   - EXIT: price drops below 10-day low

3. **Pullback to Support** — Buy dips in uptrends.
   - BUY: price pulls back to 20-day SMA from above AND RSI 40-55 AND SMA20 still rising
   - EXIT: price closes below SMA50

4. **MACD Cross** — Momentum shift detection.
   - BUY: MACD crosses above signal line AND MACD histogram turning positive AND price > SMA50
   - EXIT: MACD crosses below signal line

**Default parameters:**
- stop_loss: 5%
- take_profit: 7%
- max_position: 8% of equity
- min_volume: 500,000
- volume_surge_threshold: 2x

---

## Strategy Engine 4: Large Cap ($50-$500)

**File:** `strategy_large.py`

**Market characteristics:** Moves with the market, highly liquid, institutional-driven. Individual stock picking matters less — macro and sector rotation matter more. Lower volatility = tighter stops work.

**Strategies:**

1. **Index Correlation Buy** — When SPY bounces from support, large caps bounce too.
   - BUY: SPY RSI < 35 AND stock RSI < 40 AND stock is in SPY/QQQ
   - EXIT: SPY reaches overbought OR stock hits take-profit

2. **Relative Strength** — Buy stocks outperforming the market.
   - BUY: stock up more than SPY over 5 days AND volume > avg AND RSI < 70
   - EXIT: stock underperforms SPY for 3 consecutive days

3. **Dividend Yield Play** — Large caps with high dividend yields tend to mean-revert.
   - BUY: RSI < 35 AND stock has known dividend (blue chip)
   - EXIT: RSI > 55

4. **Moving Average Alignment** — All MAs aligned bullishly.
   - BUY: price > EMA12 > SMA20 > SMA50 AND volume > avg
   - EXIT: price closes below SMA20

**Default parameters:**
- stop_loss: 4%
- take_profit: 6%
- max_position: 7% of equity
- min_volume: 1,000,000
- volume_surge_threshold: 1.5x

---

## Strategy Engine 5: Crypto

**File:** `strategy_crypto.py`

**Market characteristics:** 24/7 trading, heavily correlated to BTC, sentiment-driven, trends hard in both directions, no earnings/fundamentals in traditional sense. Technical analysis works differently — support/resistance levels matter, social sentiment matters.

**Strategies:**

1. **BTC Correlation Play** — When BTC moves, alts follow (usually amplified).
   - BUY alt: BTC RSI < 35 AND BTC bouncing (positive 1-day change) AND alt RSI < 30
   - EXIT: BTC drops below recent low OR alt hits take-profit

2. **Trend Following** — Crypto trends harder than equities. Ride the trend.
   - BUY: price crosses above SMA20 from below AND volume > 1.5x avg AND RSI 45-65
   - EXIT: price crosses back below SMA20

3. **Extreme Oversold Bounce** — Crypto drops are extreme but bounces are violent.
   - BUY: RSI < 20 AND price > 25% below 20-day SMA
   - EXIT: RSI > 45 OR price returns to SMA20

4. **Volume Surge** — Big volume on crypto usually means something (listing, partnership, social media).
   - BUY: volume > 3x avg AND price up > 3% AND RSI < 65
   - EXIT: volume drops below avg for 2 consecutive periods

**Default parameters:**
- stop_loss: 8% (crypto is volatile)
- take_profit: 10%
- max_position: 7% of equity
- min_volume: 0 (crypto volume is measured differently)
- volume_surge_threshold: 3x

---

## Strategy Router

**File:** Update `aggressive_trader.py`

Replace the single `aggressive_combined_strategy()` call with:

```python
def run_strategy(symbol, market_type, ctx=None, df=None):
    """Route to the correct strategy engine based on market type."""
    if market_type == "micro":
        from strategy_micro import micro_combined_strategy
        return micro_combined_strategy(symbol, ctx=ctx, df=df)
    elif market_type == "small":
        from strategy_small import small_combined_strategy
        return small_combined_strategy(symbol, ctx=ctx, df=df)
    elif market_type == "midcap":
        from strategy_mid import mid_combined_strategy
        return mid_combined_strategy(symbol, ctx=ctx, df=df)
    elif market_type == "largecap":
        from strategy_large import large_combined_strategy
        return large_combined_strategy(symbol, ctx=ctx, df=df)
    elif market_type == "crypto":
        from strategy_crypto import crypto_combined_strategy
        return crypto_combined_strategy(symbol, ctx=ctx, df=df)
    else:
        from aggressive_strategy import aggressive_combined_strategy
        return aggressive_combined_strategy(symbol, df=df)
```

Each strategy engine follows the same pattern:
- 4 strategies specific to that market type
- Each returns {symbol, signal, reason, price, ...}
- Combined scoring: BUY vote +1, SELL vote -1, same as now
- Returns same dict format so AI review and execution work unchanged

## Segment Definitions Update

**File:** Update `segments.py`

Split "microsmall" into "micro" and "small":
- micro: $1-$5 universe (filter from current SMALL_CAP_UNIVERSE)
- small: $5-$20 universe (filter from current SMALL_CAP_UNIVERSE)
- midcap: unchanged
- largecap: unchanged
- crypto: unchanged

Update MARKET_TYPE_NAMES in models.py:
```python
MARKET_TYPE_NAMES = {
    "micro": "Micro Cap",
    "small": "Small Cap",
    "midcap": "Mid Cap",
    "largecap": "Large Cap",
    "crypto": "Crypto",
}
```

## Settings UI Update

- Profile creation dropdown: 5 options instead of 4
- Each market type shows its own default parameters when selected
- Existing "microsmall" profiles need migration to either "micro" or "small" based on their price range settings

## Build Order

1. Create all 5 strategy engine files
2. Create strategy router in aggressive_trader.py
3. Split segments.py universes
4. Update models.py MARKET_TYPE_NAMES
5. Migrate existing "microsmall" profiles
6. Update settings UI
7. Deploy and verify each market type produces signals
