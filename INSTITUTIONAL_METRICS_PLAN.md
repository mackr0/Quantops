# QuantOpsAI — Institutional Metrics Dashboard Plan

## Status Tracker

| # | Section | Items | Status |
|---|---|---|---|
| 1 | Executive Summary (Page 1) | Equity curve, annual return, Sharpe, Sortino, max DD, monthly table | DONE |
| 2 | Risk & Stability (Page 2) | Rolling Sharpe, rolling returns, volatility, worst periods, CVaR | DONE |
| 3 | Trade Analytics (Page 3) | Win rate, profit factor, expectancy, avg win/loss, distribution | DONE |
| 4 | Market Relationship (Page 4) | Alpha, Beta vs SPY, correlation matrix | DONE |
| 5 | Scalability (Page 5) | Liquidity analysis, slippage model, capacity limits | DONE |

---

## Architecture

Replace the current single AI Performance page with a tabbed multi-page dashboard at `/performance`.

### Navigation
```
Performance Dashboard
  [Executive Summary] [Risk & Stability] [Trade Analytics] [Market Relationship] [Scalability]
  Profile: [All Profiles ▼]
```

### Data Sources

All metrics calculated from:
- `trades` table (per-profile DB): P&L, timestamps, symbols, decision/fill prices
- `daily_snapshots` table: equity curve, daily P&L
- `ai_predictions` table: prediction accuracy
- SPY historical data (yfinance): for alpha/beta/correlation
- Live from Alpaca API: current positions, exposure

### Metrics Calculation Module

Create `metrics.py` — centralized metrics calculator:

```python
def calculate_all_metrics(db_paths, initial_capital=10000):
    """Calculate every institutional metric from trade and snapshot data.
    Returns a comprehensive dict used by all 5 dashboard pages.
    """
```

This avoids duplicating calculations across views.

---

## Page 1: Executive Summary

**The first screen investors look at.**

### Metrics to calculate:

| Metric | Formula | Target |
|---|---|---|
| Total Return % | (final_equity - initial) / initial × 100 | Positive |
| Annualized Return % | (1 + total_return)^(365/days) - 1 | 40-80% |
| Net Return | After slippage | — |
| Gross Return | Before slippage | — |
| Sharpe Ratio | mean(daily_returns) / std(daily_returns) × √252 | >2.0 |
| Sortino Ratio | mean(daily_returns) / std(negative_returns) × √252 | >2.0 |
| Max Drawdown % | max peak-to-trough decline in equity | <20% |
| Calmar Ratio | annualized_return / max_drawdown | >2.0 |

### Visual Elements:

- **Equity Curve Chart** — line chart of daily equity over time (from daily_snapshots)
  - Use inline SVG or simple HTML/CSS bar chart (no JS charting library needed)
  - Show benchmark (SPY) alongside for comparison
- **Monthly Return Table** — same as currently built but enhanced with monthly win rate
- **Key Metrics Cards** — Sharpe, Sortino, Max DD, Calmar in prominent boxes

---

## Page 2: Risk & Stability

### Metrics:

| Metric | Formula | Target |
|---|---|---|
| Annualized Volatility | std(daily_returns) × √252 | Low relative to return |
| VaR (95%) | 5th percentile of daily returns | Know worst expected day |
| CVaR (95%) | mean of returns worse than VaR | Tail risk measurement |
| Max Drawdown Duration | Days from peak to recovery | <30 days |
| Rolling 3-Month Return | Trailing 63-day return, recalculated monthly | Consistently positive |
| Rolling 6-Month Sharpe | Trailing 126-day Sharpe, recalculated monthly | Consistently >1.0 |
| Worst Periods | Worst week, worst month, worst quarter | Context for max DD |

### Visual Elements:

- **Drawdown Chart** — shows drawdown % over time (inverted equity dips)
- **Rolling Sharpe Chart** — line showing Sharpe stability
- **Worst Periods Table** — worst 5 individual periods

---

## Page 3: Trade Analytics

### Metrics:

| Metric | Formula | Target |
|---|---|---|
| Win Rate | winning_trades / total_trades | >50% |
| Profit Factor | gross_profits / gross_losses | >1.5 |
| Expectancy | (win_rate × avg_win) - (loss_rate × avg_loss) | Clearly positive |
| Avg Win $ | mean of positive P&L trades | — |
| Avg Loss $ | mean of negative P&L trades | — |
| Avg Win % | mean of positive return trades | — |
| Avg Loss % | mean of negative return trades | — |
| Win/Loss Ratio | avg_win / abs(avg_loss) | >1.0 |
| Largest Win | max P&L trade | — |
| Largest Loss | min P&L trade | — |
| Avg Hold Time | mean days between entry and exit | — |
| Trades per Month | total_trades / months_active | — |

### Visual Elements:

- **Trade P&L Distribution** — histogram of trade returns (how many at -5%, 0%, +5%, etc.)
  - Simple CSS bar chart
- **Win Rate by Strategy** — breakdown per strategy type (stop_loss, take_profit, trailing)
- **Win Rate by Market Type** — if viewing all profiles

---

## Page 4: Market Relationship

### Metrics:

| Metric | Formula | Target |
|---|---|---|
| Beta vs S&P 500 | covariance(portfolio, SPY) / variance(SPY) | <0.5 |
| Alpha | portfolio_return - (beta × SPY_return) | Positive |
| Correlation to SPY | Pearson correlation of daily returns | Low |
| Correlation to QQQ | Same for Nasdaq | Low |
| Correlation to BTC | Same for Bitcoin (if crypto profile) | Low |
| Net Exposure | (long_value - short_value) / equity | — |
| Gross Exposure | (long_value + short_value) / equity | — |

### Implementation:

- Fetch SPY, QQQ, BTC-USD daily returns from yfinance (cached)
- Calculate portfolio daily returns from daily_snapshots
- Compute correlation matrix, beta, alpha

### Visual Elements:

- **Correlation Table** — portfolio vs SPY, QQQ, BTC
- **Exposure Summary** — net long/short, gross exposure percentage

---

## Page 5: Scalability

### Metrics:

| Metric | Current | Source |
|---|---|---|
| Avg Position Size $ | From trades | — |
| Avg Position as % of Daily Volume | position_value / stock_daily_volume | <1% |
| Slippage per Trade | From slippage tracking | — |
| Total Slippage Cost | Sum of all slippage | — |
| Slippage as % of Gross Profit | total_slippage / gross_profit | <20% |
| Est. Capacity | Max AUM before slippage degrades returns | — |

### Capacity Estimation:

```
For each traded symbol:
  avg_trade_size / avg_daily_dollar_volume = impact_ratio

If impact_ratio < 0.01 (1%): scalable
If impact_ratio 0.01-0.05: moderate constraints
If impact_ratio > 0.05: liquidity limited

Est. capacity = current_AUM / max(impact_ratio) × 0.01
```

### Visual Elements:

- **Slippage Analysis** — from actual fill data
- **Capacity Table** — per symbol, how much could be traded before impacting price
- **Scaling Projection** — what happens to returns at $50K, $100K, $1M

---

## Implementation

### New file: `metrics.py`

```python
def calculate_all_metrics(db_paths, initial_capital=10000):
    """Master metrics calculator — feeds all 5 dashboard pages."""

    # Gather raw data
    trades = _gather_trades(db_paths)
    snapshots = _gather_snapshots(db_paths)

    # Performance
    total_return = ...
    annualized_return = ...

    # Risk
    sharpe = ...
    sortino = ...
    max_dd = ...
    calmar = ...
    var_95 = ...
    cvar_95 = ...
    volatility = ...

    # Trade analytics
    win_rate = ...
    profit_factor = ...
    expectancy = ...

    # Market relationship
    alpha, beta = ...
    correlations = ...

    # Scalability
    slippage_stats = ...
    capacity = ...

    return { ... everything ... }
```

### New template: `templates/performance.html`

Single page with tab navigation (CSS-only tabs, no JS framework):

```html
<div class="tabs">
    <input type="radio" name="tab" id="tab1" checked>
    <label for="tab1">Executive Summary</label>
    <input type="radio" name="tab" id="tab2">
    <label for="tab2">Risk & Stability</label>
    ...

    <div class="tab-content" id="content1">
        <!-- Executive Summary -->
    </div>
    <div class="tab-content" id="content2">
        <!-- Risk & Stability -->
    </div>
    ...
</div>
```

### Charts

Use simple inline SVG for charts — no external charting library:

```python
def render_equity_curve_svg(snapshots, width=800, height=200):
    """Generate inline SVG line chart from daily snapshots."""
```

This keeps the system dependency-free and works in emails too.

### Route

```python
@views_bp.route("/performance")
```

Keep the old `/ai-performance` route as a redirect for backward compat.

---

## Build Order

1. Create `metrics.py` with all calculations
2. Create `templates/performance.html` with 5-tab layout
3. Add route in `views.py`
4. Build each tab's content with metrics
5. Add simple SVG charts for equity curve, drawdown, rolling Sharpe
6. Update technical documentation
7. Deploy and verify with existing data
