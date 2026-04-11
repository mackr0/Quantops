# QuantOpsAI — Scaling Plan

## Current State

The system is designed and tested for **$10K paper trading accounts**. Position sizing, execution, universe selection, and risk management are all calibrated for this range.

## Scaling Milestones

### Stage 1: $10K Paper (CURRENT)
- **Status:** Active
- **Goal:** Prove the strategies generate positive returns on paper
- **Success criteria:** Consistent positive P&L over 30+ trading days with >45% trade win rate
- **What to watch:** AI prediction accuracy, self-tuning effectiveness, strategy-specific performance per market type

### Stage 2: $10K Real Money
- **Prerequisites:** Stage 1 success criteria met
- **Changes needed:**
  - Switch Alpaca from paper to live account
  - Add real-time P&L monitoring and alerts
  - Tighter drawdown limits (10% pause instead of 20%)
  - Start with 1 market type (whichever performed best on paper)
  - Monitor slippage — compare actual fills to expected prices
- **Risk budget:** Accept up to $1,000 loss (10%) as tuition before pausing

### Stage 3: $50K Real Money
- **Prerequisites:** Stage 2 profitable over 60+ trading days
- **Changes needed:**
  - Position sizing remains percentage-based (scales automatically)
  - Add minimum dollar volume filter: only trade stocks with $5M+ daily dollar volume
  - Enable limit orders by default (reduce slippage)
  - Tighter correlation limits (0.5 instead of 0.7)
  - Reduce max positions per sector to 3
  - Add VaR (Value at Risk) calculation before each trade
  - Drop micro-cap universe (too illiquid at this size)

### Stage 4: $100K-$250K
- **Prerequisites:** Stage 3 profitable over 90+ trading days
- **Changes needed:**
  - Minimum dollar volume filter: $10M+ daily
  - VWAP order execution (spread orders across time)
  - Iceberg orders (hide order size from other traders)
  - Real-time WebSocket data from Alpaca instead of 30-min polling
  - Portfolio-level risk monitoring (total exposure, sector concentration, beta)
  - Tax-lot tracking for tax optimization
  - Consider pattern day trader (PDT) rule implications

### Stage 5: $1M+
- **Prerequisites:** Stage 4 profitable over 180+ trading days
- **Changes needed:**
  - Complete execution layer rebuild:
    - Event-driven architecture (replace scheduler polling)
    - Sub-second order execution
    - Smart order routing (split large orders)
    - Market impact modeling (estimate price impact before trading)
  - Universe changes:
    - Drop small-cap entirely (insufficient liquidity)
    - Focus on mid-cap and large-cap only
    - Position sizing capped at 1% of stock's daily dollar volume
  - Risk management:
    - Real-time portfolio VaR
    - Sector exposure limits (max 20% per sector)
    - Correlation matrix updated daily
    - Maximum drawdown triggers reviewed with actual capital at risk
  - Infrastructure:
    - Dedicated server (not shared $6 droplet)
    - Redundant connections to exchange
    - Monitoring and alerting for system failures
  - Regulatory:
    - Pattern day trader compliance
    - Short selling borrowing costs
    - Potential SEC reporting requirements above certain thresholds

## What Scales Without Changes

These components work at any account size:
- **AI analysis pipeline** — same prompt, same analysis, same cost
- **Self-tuning & learning** — performance tracking is percentage-based
- **Strategy engines** — signal generation is price/volume ratio based
- **Market regime detection** — SPY/VIX analysis is account-size independent
- **Political sentiment (MAGA Mode)** — news analysis doesn't change with account size
- **Earnings calendar** — same check regardless of position size
- **Web UI & dashboards** — display layer is size-agnostic

## What Breaks at Scale

| Component | Breaks At | Why | Fix |
|---|---|---|---|
| Market orders | $50K+ | Slippage on small caps | Limit/VWAP orders |
| Micro-cap universe | $50K+ | Orders are >1% of daily volume | Drop micro-caps |
| Small-cap universe | $250K+ | Same liquidity issue | Raise volume floors |
| 30-min scan interval | $250K+ | Too slow, miss opportunities | Real-time streaming |
| Correlation (0.7) | $100K+ | Correlated losses too large | Tighten to 0.5 |
| Fixed % position sizing | $1M+ | % of equity exceeds % of daily volume | Volume-based sizing |
| Scheduler architecture | $1M+ | Polling too slow | Event-driven |
| Shared $6 droplet | $100K+ | Single point of failure | Dedicated infrastructure |

## Decision Framework

At each stage, ask:
1. Is the strategy still generating positive returns? → Continue
2. Is slippage eating more than 20% of gross profits? → Fix execution
3. Are position sizes exceeding 1% of daily stock volume? → Tighten universe
4. Is drawdown exceeding the stage-appropriate limit? → Pause and review
5. Has the market regime changed significantly? → Validate strategy still works

## Timeline Estimate

| Stage | Duration | Cumulative |
|---|---|---|
| Stage 1 (paper) | 1-3 months | 1-3 months |
| Stage 2 ($10K real) | 2-3 months | 3-6 months |
| Stage 3 ($50K) | 3-6 months | 6-12 months |
| Stage 4 ($100K-$250K) | 6-12 months | 12-24 months |
| Stage 5 ($1M+) | Ongoing | 18+ months |

Each stage should only begin after the previous stage's success criteria are met. Rushing ahead with unproven strategies at larger scale is how accounts blow up.
