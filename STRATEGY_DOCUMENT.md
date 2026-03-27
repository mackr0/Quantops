# Quantops — Trading & AI Strategy Document

**Version:** 2.0
**Date:** March 27, 2026
**Purpose:** Paper trading experiment — $300K total virtual capital across three accounts
**Asset classes:** US equities across three market cap segments (small, mid, large)

---

## 1. System Overview

Quantops is an autonomous paper trading system that combines rules-based technical analysis with AI-powered trade review. It operates three independent Alpaca paper trading accounts — one each for small-cap, mid-cap, and large-cap stocks — applying the same core strategy with segment-tuned parameters.

The system screens ~600+ stocks across all segments, applies four independent technical strategies, then sends each actionable signal through a Claude AI review before execution. Every AI prediction is tracked against actual price outcomes to measure whether the AI adds value.

```
┌──────────────────────────────────────────────────────────────────┐
│                      QUANTOPS PIPELINE                           │
│                                                                  │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐                    │
│  │ Small Cap │   │ Mid Cap  │   │ Large Cap│   3 Alpaca accounts │
│  │  $100K   │   │  $100K   │   │  $100K   │                    │
│  │ 259 stocks│   │ 191 stocks│   │ 163 stocks│                    │
│  └────┬─────┘   └────┬─────┘   └────┬─────┘                    │
│       │              │              │                            │
│       └──────────────┼──────────────┘                            │
│                      ▼                                           │
│            Yahoo Finance Data (free)                             │
│                      ▼                                           │
│         4 Technical Strategies (scoring)                         │
│                      ▼                                           │
│          Claude AI Review (approve/veto)                         │
│                      ▼                                           │
│         Execute on Alpaca (paper trades)                         │
│                      ▼                                           │
│     Track AI accuracy + email notifications                      │
│                      ▼                                           │
│    Compare performance: small vs mid vs large                    │
└──────────────────────────────────────────────────────────────────┘
```

**Data source:** Yahoo Finance (free, no API key, full historical data)
**Execution:** Alpaca Paper Trading API (commission-free, simulated fills)
**AI model:** Claude Sonnet (Anthropic) — analytical review layer, not a signal generator
**Infrastructure:** DigitalOcean droplet ($6/mo), runs autonomously 24/7

---

## 2. Multi-Account Segment Architecture

Three independent paper trading accounts run the same strategy with segment-specific parameters:

| Parameter | Small Cap | Mid Cap | Large Cap |
|---|---|---|---|
| **Virtual capital** | $100,000 | $100,000 | $100,000 |
| **Price range** | $1 – $20 | $20 – $100 | $50 – $500 |
| **Min daily volume** | 500,000 | 300,000 | 1,000,000 |
| **Universe size** | ~260 stocks | ~190 stocks | ~160 stocks |
| **Max position size** | 10% of equity | 8% of equity | 7% of equity |
| **Stop-loss** | 3% | 4% | 5% |
| **Take-profit** | 10% | 12% | 15% |
| **Max positions** | 10 | 10 | 10 |
| **Database** | quantops_smallcap.db | quantops_midcap.db | quantops_largecap.db |

**Rationale for parameter differences:**
- **Wider stops for larger caps:** Large-cap stocks are less volatile; a 3% stop would trigger on normal intraday noise. The 5% stop gives blue chips room to breathe while still cutting clear trend failures.
- **Higher take-profit for larger caps:** Large-cap moves tend to be slower but more sustained. A 15% target captures meaningful swings without exiting too early.
- **Larger positions for small caps:** Higher volatility = more opportunity per trade, so we allocate more aggressively. Large-cap positions are smaller because the upside per trade is more modest.
- **Separate databases:** Each segment's trade journal, AI predictions, and performance metrics are tracked independently, allowing direct comparison of strategy effectiveness across market cap segments.

---

## 3. Stock Universes

### Small Cap (~260 stocks, $1–$20)

Curated liquid names across:
- **Fintech:** SOFI, HOOD, AFRM, UPST, CLOV, OPEN, PSFE, LMND
- **EVs & mobility:** RIVN, LCID, NIO, XPEV, LI, NKLA, QS, CHPT, BLNK, EVGO
- **Social / tech:** SNAP, PATH, BB, NOK, GENI, AI, BBAI, SOUN, RKLB
- **Cannabis:** TLRY, CGC, ACB, SNDL
- **Crypto miners:** MARA, RIOT, HUT, BITF, CIFR, CLSK, IREN, WULF
- **Clean energy:** PLUG, FCEL, BE, RUN, ARRY, STEM, JKS, DQ
- **Oil & gas:** RIG, ET, AR, CNX, BTU, KOS, BTE
- **Airlines / travel:** JBLU, AAL, NCLH, CCL
- **Biotech:** DNA, ADMA, WVE, OLPX, HIMS, CRSP, NTLA, BEAM
- **Consumer:** CAVA, SHAK, BROS, ELF, CELH
- **Mining:** GOLD, HL, CDE, AG, PAAS
- **REITs:** AGNC, NLY, TWO, MFA, IVR
- **Aerospace:** JOBY, ACHR, ASTS, LUNR

### Mid Cap (~190 stocks, $20–$100)

- **SaaS / cloud:** DDOG, NET, ZS, BILL, HUBS, MDB, GTLB, DOCN, MNDY
- **Consumer tech:** ROKU, PINS, ETSY, CHWY, CVNA, LYFT
- **Fintech:** SQ, COIN, AFRM, ALLY, AXOS, LC
- **Cybersecurity:** CRWD, S, TENB, RPD
- **Gaming / entertainment:** DKNG, RBLX, PENN, U
- **Semiconductors:** ON, WOLF, LSCC, ACLS
- **Ad tech / digital:** TTD, APP, MGNI, PUBM
- **Healthcare:** HIMS, DOCS, GDRX, OSCR, ACCD, SDGR, BLI, TXG
- **Consumer brands:** LULU, DECK, SKX, FIVE, OLLI, DKS, ELF, CROX, BIRK
- **EVs / transport:** RIVN, JOBY, LYFT
- **Quantum / AI:** IONQ, RGTI, QUBT

### Large Cap (~160 stocks, $50–$500)

- **Mega-tech:** AAPL, MSFT, GOOGL, AMZN, META, NVDA, TSLA
- **Semiconductors:** AMD, INTC, QCOM, AVGO, TXN, MU
- **Software:** CRM, ORCL, ADBE, NOW, INTU, WDAY
- **Streaming / media:** NFLX, DIS, CMCSA
- **Telecom:** T, VZ, TMUS
- **Financials:** JPM, BAC, GS, MS, WFC, V, MA, AXP, BLK, SCHW
- **Healthcare:** UNH, JNJ, PFE, MRK, LLY, AMGN, GILD, ISRG, ABT, TMO
- **Industrials:** BA, RTX, LMT, GE, HON, CAT, DE
- **Consumer:** COST, WMT, TGT, HD, LOW, SBUX, MCD, NKE, KO, PEP
- **Travel:** BKNG, MAR, HLT, DAL, UAL, LUV

---

## 4. Technical Strategies

Four independent strategies each cast a BUY, SELL, or HOLD vote. These are identical across all three segments — only the risk parameters differ.

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

## 5. AI Review Layer

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

## 6. Position Sizing & Risk Management

### Position Sizing by Segment

| Signal | Small Cap | Mid Cap | Large Cap |
|---|---|---|---|
| **STRONG_BUY** | 10% of equity | 8% of equity | 7% of equity |
| **BUY** | 7.5% of equity | 6% of equity | 5.25% of equity |
| **AI confidence ≥ 80%** | Up to 1.25× base (capped at max) | Same | Same |

### Exit Rules by Segment

| Trigger | Small Cap | Mid Cap | Large Cap | Rationale |
|---|---|---|---|---|
| **Stop-loss** | -3% | -4% | -5% | Tighter for volatile small caps, wider for stable large caps |
| **Take-profit** | +10% | +12% | +15% | Higher targets for larger caps where trends are more sustained |
| **Strategy SELL** | Sell 75–100% | Same | Same | Technical exit when conditions deteriorate |

### Risk/Reward Ratios

| Segment | Stop-Loss | Take-Profit | Risk:Reward |
|---|---|---|---|
| Small Cap | 3% | 10% | 1:3.3 |
| Mid Cap | 4% | 12% | 1:3.0 |
| Large Cap | 5% | 15% | 1:3.0 |

All segments target approximately 1:3 risk/reward, meaning a 33% win rate would break even before fees.

### Portfolio Constraints (All Segments)

- Maximum 10 open positions per account
- No position exceeding segment max % of equity (including additions to existing positions)
- Trade value cannot exceed available cash
- Sell orders are always permitted (risk reduction)
- Stop-loss and take-profit checked every 15 minutes during market hours

---

## 7. AI Performance Tracking

Every AI prediction is stored in a per-segment SQLite database and resolved against actual price movements over time.

### Resolution Criteria

| AI Prediction | WIN | LOSS | Timeout |
|---|---|---|---|
| BUY | Price rises ≥ 5% from prediction price | Price drops ≥ 3% | After 20 trading days → neutral |
| SELL | Price drops ≥ 5% | Price rises ≥ 3% | After 20 trading days → neutral |
| HOLD | Price change < 3% after 5 trading days | Price change ≥ 3% after 5 days | After 20 trading days → neutral |

### Metrics Tracked (Per Segment)

- **Win rate** — overall and by signal type (BUY/SELL/HOLD)
- **Average confidence on wins vs. losses** — does higher confidence correlate with better outcomes?
- **Accuracy by confidence band** — 0–25%, 25–50%, 50–75%, 75–100%
- **Average return on AI BUY vs. SELL predictions**
- **Profit factor** — gross gains ÷ gross losses
- **Best and worst individual predictions**

This allows us to answer: *Is the AI adding value? Does it perform differently across market cap segments?*

---

## 8. Autonomous Operation

### Schedule (US Market Hours, Mon–Fri)

| Interval | Task | Applies To |
|---|---|---|
| Every 15 minutes | Check all positions against stop-loss and take-profit | All 3 segments |
| Every 30 minutes | Full pipeline: screen → technical analysis → AI review → execute | All 3 segments (sequential) |
| Every 60 minutes | Resolve pending AI predictions against current prices | All 3 segments |
| 3:55 PM ET daily | Save portfolio snapshot + send daily summary email | All 3 segments |

### Execution Sequence Per Cycle

Each 30-minute cycle processes segments sequentially:
1. **Small Cap** — screen 260 stocks, analyze candidates, AI review, execute trades
2. **Mid Cap** — screen 190 stocks, analyze candidates, AI review, execute trades
3. **Large Cap** — screen 160 stocks, analyze candidates, AI review, execute trades

Each segment temporarily loads its own Alpaca credentials, database, and risk parameters.

### Infrastructure

- **Server:** DigitalOcean droplet (1 vCPU, 1GB RAM, Ubuntu 24.04) — $6/mo
- **Process manager:** systemd (auto-restart on failure, starts on boot)
- **Notifications:** Email via Resend API

### Email Notifications

| Email Type | When | Content |
|---|---|---|
| **Trade executed** | On each buy/sell | Symbol, qty, price, AI analysis, account snapshot, positions |
| **AI veto** | When AI blocks a BUY | Technical signal vs. AI signal, reasoning |
| **Stop-loss / take-profit** | When exit triggers | Exit details, P&L, remaining positions |
| **Daily summary** | 3:55 PM ET | All 3 accounts: equity, positions, trades, AI accuracy |

---

## 9. Technical Indicators Used

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

## 10. Known Limitations & Risks

1. **Small-cap volatility:** These stocks can move 10–20% in a day. The 3% stop-loss may trigger frequently in normal volatility, leading to whipsaw losses.

2. **AI non-determinism:** Claude's responses are probabilistic. The same data may produce different signals on different calls. This is mitigated by using AI as a gate rather than a signal generator.

3. **No fundamental analysis:** The system is purely technical. It does not consider earnings, news, SEC filings, or macroeconomic factors (except insofar as the AI may incorporate general knowledge).

4. **Free data limitations:** Yahoo Finance data may have slight delays (15–20 minutes for some quotes). This matters less for 30-minute scan intervals but could affect intraday strategies.

5. **Paper trading divergence:** Paper fills are simulated and may not reflect real market conditions (slippage, partial fills, market impact on low-float stocks).

6. **Curated universe bias:** The stock universes are manually selected. Stocks not in a segment's universe are invisible to the system regardless of opportunity.

7. **Sequential processing:** The three segments are processed one after another, not in parallel. A full cycle takes ~10–15 minutes. A stock that qualifies at cycle start may have moved by the time the order is placed.

8. **Same strategy, different markets:** The four technical strategies were designed with small-cap volatility in mind. They may generate fewer actionable signals in the large-cap space where moves are more gradual.

9. **AI cost scaling:** Each AI review costs ~$0.01–0.03 per call. With three segments running 13 scan cycles per day, AI costs could reach $5–15/day depending on the number of actionable signals.

---

## 11. Experiment Goals

The primary questions this paper trading experiment aims to answer:

1. **Does the AI gate improve returns?** Compare AI-approved trades vs. what would have happened without the gate (tracked via prediction resolution).

2. **Which market cap segment performs best?** With the same strategy and AI model, does the system generate more alpha in small, mid, or large caps?

3. **Does AI confidence predict outcomes?** If 80%+ confidence predictions outperform 25–50% confidence predictions, the system should weight confidence into position sizing.

4. **What is the optimal stop-loss/take-profit?** The current 1:3 risk/reward ratio is a starting point. Actual trade data will reveal whether tighter or wider stops improve outcomes per segment.

5. **Which technical strategies contribute most?** The four sub-strategies can be individually evaluated. If one consistently generates false signals, it can be removed or reweighted.

---

## 12. Current Configuration Summary

```
Total Virtual Capital:    $300,000 (3 × $100,000 paper accounts)
AI Model:                 Claude Sonnet (claude-sonnet-4-20250514)
AI Min Confidence:        25% (for BUY approval)
Total Universe:           ~610 stocks across 3 segments
Scan Frequency:           Every 30 minutes during market hours
Exit Check Frequency:     Every 15 minutes during market hours
Server:                   DigitalOcean droplet ($6/mo)
Data Source:              Yahoo Finance (free)
Execution:                Alpaca Paper Trading (free)
Notifications:            Email via Resend API

Small Cap:  259 stocks | $1-$20  | 3% stop | 10% TP | 10% position
Mid Cap:    191 stocks | $20-$100 | 4% stop | 12% TP | 8% position
Large Cap:  163 stocks | $50-$500 | 5% stop | 15% TP | 7% position
```

---

*This document describes a paper trading experiment. No real capital is at risk. The system is designed to test whether AI-augmented technical analysis can generate alpha across different market cap segments.*
