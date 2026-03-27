# Quantops — Trading & AI Strategy Document

**Version:** 1.0
**Date:** March 27, 2026
**Purpose:** Paper trading only ($100K virtual account via Alpaca)
**Asset class:** US equities — small-cap and micro-cap stocks ($1–$20, 500K+ daily volume)

---

## 1. System Overview

Quantops is an autonomous paper trading system that combines rules-based technical analysis with AI-powered trade review. The system screens ~300 small/micro-cap stocks, applies four independent technical strategies, then sends each actionable signal through a Claude AI review before execution. Every AI prediction is tracked against actual outcomes to measure whether the AI improves returns.

**Data source:** Yahoo Finance (free, real-time quotes, full historical data)
**Execution:** Alpaca Paper Trading API (commission-free, simulated fills)
**AI model:** Claude Sonnet (Anthropic) — used as an analytical review layer, not a signal generator

---

## 2. Universe & Screening

### Stock Universe
A curated list of ~300 liquid small/micro-cap names across sectors:
- Fintech (SOFI, HOOD, AFRM, UPST)
- EVs & mobility (RIVN, LCID, NIO, XPEV, CHPT)
- Crypto miners (MARA, RIOT, HUT, CLSK)
- Clean energy (PLUG, FCEL, BE, RUN)
- Oil & gas (RIG, KOS, BTE, AR)
- Airlines & travel (JBLU, AAL, NCLH, CCL)
- Biotech (DNA, ADMA, WVE, HIMS)
- Plus mining, REITs, consumer, aerospace, telecom

### Screening Filters (applied every 30 minutes during market hours)
1. **Price range:** $1.00 – $20.00
2. **Minimum volume:** 500,000 shares/day
3. **Volume surge detection:** Today's volume ≥ 2× 20-day average
4. **Momentum screen:** ≥ 3% gain over 5 days AND ≥ 5% gain over 20 days
5. **Breakout detection:** Price above 20-day high on above-average volume

Data is fetched via yfinance batch download (single HTTP request for all symbols).

---

## 3. Technical Strategies

Four independent strategies each cast a BUY, SELL, or HOLD vote. Votes are summed into a score.

### Strategy 1: Momentum Breakout

| | Condition |
|---|---|
| **BUY** | Price breaks above 20-day high AND volume > 1.5× 20-day average AND RSI between 50–80 |
| **SELL** | Price drops below 10-day low OR RSI > 85 (exhaustion) |
| **HOLD** | Neither condition met |

**Rationale:** Classic breakout strategy. Requires volume confirmation to filter false breakouts. RSI 50–80 ensures momentum is present but not exhausted. The 10-day low stop gives the trade room to breathe while cutting clear trend failures.

### Strategy 2: Volume Spike

| | Condition |
|---|---|
| **BUY** | Volume > 2× 20-day average AND price up > 2% intraday AND RSI < 70 |
| **SELL** | Volume below 20-day average AND 2 consecutive red candles (close < open) |
| **HOLD** | Neither condition met |

**Rationale:** Abnormal volume with price appreciation suggests institutional interest or a catalyst. The RSI < 70 filter avoids chasing already-extended moves. The sell signal detects fading interest with consecutive distribution days.

### Strategy 3: Mean Reversion (Aggressive)

| | Condition |
|---|---|
| **BUY** | RSI < 25 AND price > 10% below 20-day SMA |
| **SELL** | Price returns to 20-day SMA OR RSI > 60 |
| **HOLD** | Neither condition met |

**Rationale:** Deeply oversold bounce play. Requires both RSI extreme (< 25) and significant deviation from the mean (> 10% below SMA) to confirm oversold conditions rather than a fundamental breakdown. Exits when the mean reversion target is achieved (SMA) or momentum normalizes (RSI > 60).

### Strategy 4: Gap and Go

| | Condition |
|---|---|
| **BUY** | Today's open > 3% above yesterday's close AND volume above 20-day average |
| **SELL** | Gap > 3% occurred but price drops below today's open (gap fill) |
| **HOLD** | No significant gap or gap without volume confirmation |

**Rationale:** Opening gaps with volume confirmation often indicate overnight catalysts (earnings, news, upgrades). The gap-fill exit protects against failed gaps — if price can't hold the gap, the thesis is broken.

### Combined Scoring

Each strategy votes independently:
- BUY vote = **+1**
- SELL vote = **-1**
- HOLD vote = **0**

| Total Score | Final Signal |
|---|---|
| ≥ 2 | **STRONG_BUY** |
| 1 | **BUY** |
| 0 | **HOLD** (no action) |
| -1 | **SELL** |
| ≤ -2 | **STRONG_SELL** |

This multi-strategy voting system reduces false signals — a trade requires at least one strategy to trigger while no other strategy actively disagrees.

---

## 4. AI Review Layer

### How It Works

Before any order is submitted, the system sends the stock's technical data to Claude (Anthropic's AI model) for an independent analysis. This acts as a **second opinion gate**, not a primary signal generator.

### Data Sent to AI

For each stock under review, Claude receives:
- Current price
- SMA-20 and SMA-50 (and their relationship)
- EMA-12
- RSI (14-period)
- MACD, MACD signal line, and histogram
- Bollinger Bands (upper, lower, middle)
- 20-day volume average
- Last 10 closing prices (price action context)
- Last 10 daily volumes (volume trend context)

### AI Prompt

The AI is instructed:
> "You are a quantitative trading analyst. Analyze the following technical data and provide a trading recommendation."

It must respond with structured JSON containing:
- **signal:** BUY, SELL, or HOLD
- **confidence:** 0–100 integer
- **reasoning:** Written explanation of the analysis
- **risk_factors:** List of identified risks
- **price_targets:** Entry, stop-loss, and take-profit levels

### Veto Rules

The AI cannot initiate trades — it can only approve or veto signals from the technical strategies:

| Technical Signal | AI Response | Result |
|---|---|---|
| BUY | AI says SELL | **VETOED** — order not placed |
| BUY | AI confidence < 25% and AI ≠ BUY | **VETOED** — insufficient conviction |
| BUY | AI says BUY or HOLD with ≥ 25% confidence | **APPROVED** — order placed |
| SELL | AI says BUY with ≥ 70% confidence | **VETOED** — AI strongly disagrees |
| SELL | Any other AI response | **APPROVED** — order placed |

### Why AI as a Gate (Not a Signal Generator)

1. **Deterministic core:** The technical strategies are rules-based and backtestable. Adding AI as a primary signal generator would make the system non-deterministic and impossible to backtest.
2. **Measurable value-add:** By tracking approval/veto outcomes separately, we can quantify whether the AI gate improves returns vs. pure technical trading.
3. **Guardrail function:** The AI catches context that rules miss — e.g., a stock might be technically oversold but the AI recognizes it's in a sector-wide collapse with no catalyst for reversal.

---

## 5. Risk Management

### Position Sizing

| Parameter | Value |
|---|---|
| Maximum position size | 10% of equity per position |
| STRONG_BUY allocation | 10% of equity |
| BUY allocation | 7.5% of equity |
| High AI confidence boost (≥ 80%) | Up to 1.25× base allocation (capped at 10%) |
| Maximum total positions | 10 |
| Minimum price | $1.00 |
| Maximum price | $20.00 |
| Minimum daily volume | 500,000 shares |

### Exit Rules

| Trigger | Action | Rationale |
|---|---|---|
| **Stop-loss: -3%** | Sell entire position | Small-caps are volatile; cut losses fast before they compound |
| **Take-profit: +10%** | Sell entire position | Capture gains before mean reversion; asymmetric 3:1 risk/reward |
| **Strategy SELL signal** | Sell 75% (SELL) or 100% (STRONG_SELL) | Technical exit when conditions deteriorate |

### Portfolio Constraints

- No new position if already at 10 open positions
- No position exceeding 10% of equity (including existing holdings in same symbol)
- Trade value cannot exceed available cash
- Sell orders are always permitted (risk reduction)

---

## 6. AI Performance Tracking

Every AI prediction is stored in a SQLite database and resolved against actual price movements over time.

### Resolution Criteria

| AI Prediction | WIN | LOSS | Timeout |
|---|---|---|---|
| BUY | Price rises ≥ 5% from prediction price | Price drops ≥ 3% | After 20 trading days → neutral |
| SELL | Price drops ≥ 5% | Price rises ≥ 3% | After 20 trading days → neutral |
| HOLD | Price change < 3% after 5 trading days | Price change ≥ 3% after 5 days | After 20 trading days → neutral |

### Metrics Tracked

- **Win rate** (overall and by signal type)
- **Average confidence on wins vs. losses** (does higher confidence correlate with better outcomes?)
- **Accuracy by confidence band** (0–25%, 25–50%, 50–75%, 75–100%)
- **Average return on AI BUY vs. SELL predictions**
- **Profit factor** (gross gains ÷ gross losses)
- **Best and worst individual predictions**

This allows us to answer: *Is the AI actually adding value, or would we do better without it?*

---

## 7. Autonomous Operation

### Schedule (US Market Hours)

| Interval | Task |
|---|---|
| Every 15 minutes | Check all open positions against stop-loss (-3%) and take-profit (+10%) |
| Every 30 minutes | Full pipeline: screen → technical analysis → AI review → execute |
| Every 60 minutes | Resolve pending AI predictions against current prices |
| 3:55 PM ET daily | Save portfolio snapshot + send daily summary email |

### Infrastructure

- **Server:** DigitalOcean droplet (1 vCPU, 1GB RAM, Ubuntu 24.04)
- **Process manager:** systemd (auto-restart on failure, starts on boot)
- **Notifications:** Email via Resend API (trade alerts, AI vetoes, stop-loss triggers, daily summary)

---

## 8. Technical Indicators Used

| Indicator | Period | Purpose |
|---|---|---|
| SMA (Simple Moving Average) | 20, 50 | Trend direction and mean reversion targets |
| EMA (Exponential Moving Average) | 12 | Short-term trend sensitivity |
| RSI (Relative Strength Index) | 14 | Overbought/oversold conditions |
| MACD | 12/26/9 | Momentum and trend change detection |
| Bollinger Bands | 20, 2σ | Volatility and price channel extremes |
| Volume SMA | 20 | Volume anomaly detection |
| Rolling High/Low | 10, 20 | Breakout and breakdown levels |

---

## 9. Known Limitations & Risks

1. **Small-cap volatility:** These stocks can move 10–20% in a day. The 3% stop-loss may trigger frequently in normal volatility, leading to whipsaw losses.

2. **AI non-determinism:** Claude's responses are probabilistic. The same data may produce different signals on different calls. This is mitigated by using AI as a gate rather than a signal generator.

3. **No fundamental analysis:** The system is purely technical. It does not consider earnings, news, SEC filings, or macroeconomic factors (except insofar as the AI may incorporate general knowledge).

4. **Free data limitations:** Yahoo Finance data may have slight delays (15–20 minutes for some quotes). This matters less for 30-minute scan intervals but could affect intraday strategies.

5. **Paper trading divergence:** Paper fills are simulated and may not reflect real market conditions (slippage, partial fills, market impact on low-float stocks).

6. **Curated universe bias:** The ~300-symbol universe is manually selected. Stocks not in the universe are invisible to the system regardless of opportunity.

7. **Single-day holding bias:** The aggressive strategies are designed for short-term trades. In trending markets, the 10% take-profit may exit too early; in choppy markets, the 3% stop-loss may exit too often.

---

## 10. Current Configuration Summary

```
Account:            Alpaca Paper Trading ($100,000 virtual)
AI Model:           Claude Sonnet (claude-sonnet-4-20250514)
AI Min Confidence:  25% (for BUY approval)
Stop-Loss:          3%
Take-Profit:        10%
Max Position:       10% of equity
Max Positions:      10
Screen Universe:    ~300 small/micro-cap stocks
Screen Price Range: $1.00 – $20.00
Screen Min Volume:  500,000 shares/day
Scan Frequency:     Every 30 minutes during market hours
Exit Check:         Every 15 minutes during market hours
```

---

*This document describes a paper trading experiment. No real capital is at risk. The system is designed to test whether AI-augmented technical analysis can generate alpha in the small-cap space.*
