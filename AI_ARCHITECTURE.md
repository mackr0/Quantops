# QuantOpsAI — AI Architecture Map

How the system uses AI, every agent and its purpose, and how they flow together.

---

## Overview

QuantOpsAI makes **7 distinct types of AI calls** per scan cycle. Each serves a different analytical function. They execute in a pipeline — each stage feeds the next.

```
                         ┌──────────────────────┐
                         │   MARKET DATA FEED    │
                         │  (Alpaca + yfinance)  │
                         └──────────┬───────────┘
                                    │
                         ┌──────────▼───────────┐
                         │   SCREENER            │
                         │   8,000+ stocks → 15  │
                         │   (no AI — rule-based) │
                         └──────────┬───────────┘
                                    │
                    ┌───────────────▼───────────────┐
                    │   MULTI-STRATEGY SCORING       │
                    │   16 strategies vote BUY/SELL   │
                    │   (no AI — rule-based)          │
                    └───────────────┬───────────────┘
                                    │
              ┌─────────────────────▼─────────────────────┐
              │          SPECIALIST ENSEMBLE (4 AI calls)  │
              │                                            │
              │  ┌──────────┐ ┌──────────┐ ┌──────────┐  │
              │  │ Earnings  │ │ Pattern  │ │Sentiment │  │
              │  │ Analyst   │ │Recognizer│ │Narrative │  │
              │  └────┬─────┘ └────┬─────┘ └────┬─────┘  │
              │       │            │             │         │
              │  ┌────▼────────────▼─────────────▼────┐   │
              │  │          Risk Assessor              │   │
              │  │      (has VETO authority)            │   │
              │  └────────────────┬────────────────────┘   │
              │                   │                        │
              │    Consensus verdict per candidate         │
              └─────────────────────┬─────────────────────┘
                                    │
                    ┌───────────────▼───────────────┐
                    │   BATCH TRADE SELECTOR         │
                    │   (1 AI call)                   │
                    │                                 │
                    │   Sees ALL candidates + context: │
                    │   • Ensemble verdicts            │
                    │   • Portfolio state              │
                    │   • Market regime (VIX, SPY)     │
                    │   • Political context            │
                    │   • Learned patterns             │
                    │   • Per-stock track record       │
                    │                                  │
                    │   Picks 0-3 trades + sizes them  │
                    └───────────────┬───────────────┘
                                    │
                         ┌──────────▼───────────┐
                         │   ORDER EXECUTION     │
                         │   (Alpaca API)         │
                         └──────────┬───────────┘
                                    │
                         ┌──────────▼───────────┐
                         │   INTERNAL LEDGER     │
                         │   (SQLite per-profile) │
                         └──────────────────────┘
```

---

## The 7 AI Agent Types

### 1. Earnings Analyst
- **Purpose:** Evaluate financial fundamentals — recent earnings, revenue trends, guidance
- **When:** Every scan cycle, on each batch of ~5 candidates
- **Model:** Claude Haiku (cheapest, fastest)
- **Output:** BUY / SELL / HOLD / ABSTAIN with confidence score + reasoning
- **Abstains** when it has no earnings data for a symbol (rather than guessing)
- **Cost label:** `ensemble:earnings_analyst`

### 2. Pattern Recognizer
- **Purpose:** Read technical chart patterns — price action, volume patterns, support/resistance, momentum
- **When:** Every scan cycle, on each batch of ~5 candidates
- **Model:** Claude Haiku
- **Output:** BUY / SELL / HOLD with confidence + reasoning
- **Cost label:** `ensemble:pattern_recognizer`

### 3. Sentiment & Narrative Analyst
- **Purpose:** Evaluate the story around the stock — news headlines, sector headwinds/tailwinds, macro environment
- **When:** Every scan cycle, on each batch of ~5 candidates
- **Model:** Claude Haiku
- **Output:** BUY / SELL / HOLD with confidence + reasoning
- **Cost label:** `ensemble:sentiment_narrative`

### 4. Risk Assessor
- **Purpose:** The skeptic. Evaluates concentration risk, regulatory exposure, event risk, regime sensitivity
- **When:** Every scan cycle, on each batch of ~5 candidates
- **Model:** Claude Haiku
- **Output:** BUY / SELL / HOLD / **VETO** with confidence + reasoning
- **Special power:** Can VETO a trade outright regardless of what the other 3 specialists say. Used sparingly — only for genuine structural risks, not routine caution.
- **Cost label:** `ensemble:risk_assessor`

### 5. Batch Trade Selector (The Portfolio Manager)
- **Purpose:** The final decision-maker. Sees all candidates with their ensemble verdicts, the current portfolio, market regime, and learned patterns. Picks which trades to actually execute and sizes them.
- **When:** Once per scan cycle (after all specialists have voted)
- **Model:** Claude Haiku (configurable per profile)
- **Input:** 
  - All ~15 candidates with full indicator data
  - Ensemble verdicts from the 4 specialists
  - Current portfolio holdings and P&L
  - Market regime (VIX level, SPY trend, crisis state)
  - Political context (if MAGA mode enabled)
  - Per-stock track record (past wins/losses on each symbol)
  - Learned patterns from self-tuning
- **Output:** JSON array of 0-3 trades, each with: symbol, action (BUY/SELL), size (% of equity), confidence, reasoning
- **Cost label:** `batch_select`

### 6. Political Context Analyst
- **Purpose:** Analyze current political news, tariff developments, executive orders — and flag stocks that could be affected (positively or negatively)
- **When:** Once per scan cycle (if MAGA mode enabled on the profile)
- **Model:** Claude Haiku
- **Output:** Text summary injected into the batch selector's context
- **Cost label:** `political_context`
- **Skipped** for crypto profiles (politics don't move Bitcoin the same way)

### 7. Strategy Proposer
- **Purpose:** Generate new strategy ideas as JSON specifications. These are validated, backtested, and promoted through a lifecycle (proposed → validated → shadow → active → retired).
- **When:** Weekly (Sundays), not every cycle
- **Model:** Claude Haiku
- **Output:** JSON strategy spec against a closed allowlist — AI never writes code, only parameters
- **Safety:** Invalid specs are silently dropped. Max 5 auto-strategies per profile.
- **Cost label:** `strategy_proposal`

---

## Supporting AI Calls (not every cycle)

### Single-Symbol Analyzer
- **Purpose:** Deep-dive analysis on one specific stock (used by the event-driven system when a price shock or SEC filing triggers a reactive analysis)
- **When:** On-demand, triggered by events
- **Cost label:** `single_analyze`

### Consensus Secondary Model
- **Purpose:** A second AI model (different provider, e.g., GPT-4o-mini) reviews the primary AI's trade selections for a second opinion
- **When:** Every cycle, only if "Multi-Model Consensus" is enabled on the profile
- **Cost label:** `consensus_secondary`

### SEC Filing Diff Analyzer
- **Purpose:** When a new SEC filing (10-K, 10-Q, 8-K) is detected, the AI reads the filing and compares it to the previous version, flagging material language changes
- **When:** When new filings are detected (event-driven)
- **Cost label:** `sec_diff`

### Portfolio Review
- **Purpose:** Periodic holistic review of the entire portfolio — are positions still justified? Any correlated risks building up?
- **When:** Less frequent than regular scans
- **Cost label:** `portfolio_review`

---

## Cost Per Scan Cycle

| Agent | Calls per Cycle | Est. Cost |
|---|---|---|
| Earnings Analyst | 3 (chunked by 5 candidates) | $0.003 |
| Pattern Recognizer | 3 | $0.003 |
| Sentiment & Narrative | 3 | $0.003 |
| Risk Assessor | 3 | $0.003 |
| Batch Trade Selector | 1 | $0.001 |
| Political Context | 1 (if MAGA mode) | $0.001 |
| **Total per cycle** | **~13-14** | **~$0.014** |

At 4 cycles/hour × 6.5 market hours × 10 profiles:
- **~$3.64/day** or **~$109/month** at full load

---

## How They Work Together

1. **Screener** (no AI) filters 8,000+ stocks to ~15 candidates based on price, volume, and technical rules.

2. **16 Strategies** (no AI) each independently vote BUY/SELL/HOLD on each candidate. Votes are aggregated into a score.

3. **4 Specialist AIs** each evaluate the ~15 candidates from their unique perspective. They're batched in chunks of 5 to prevent the AI from dropping entries. Each returns a verdict + confidence + reasoning.

4. **The ensemble synthesizer** (code, not AI) combines the 4 specialist verdicts into a consensus using confidence-weighted voting. The Risk Assessor's VETO overrides everything.

5. **The Batch Trade Selector** (1 AI call) sees the full picture — candidates, ensemble verdicts, portfolio state, market regime — and makes the final call on which 0-3 trades to execute and how to size them.

6. **Order execution** sends the trades to Alpaca. The internal ledger records everything for metrics, self-tuning, and the meta-model.

---

## Self-Learning Loop

The AI isn't static. Three feedback mechanisms improve it over time:

### Self-Tuning (daily)
Watches the AI's win rate and adjusts parameters (confidence threshold, stop/TP percentages). Reviews its own past adjustments and reverses ones that made things worse.

### Meta-Model (Phase 1 — collecting data)
A gradient-boosted classifier trained on the AI's own prediction history. Learns patterns like "the AI is overconfident on low-volume mid-caps in sideways markets" and adjusts confidence before execution. Needs 100+ resolved predictions to train.

### Alpha Decay Monitor (continuous)
Tracks each strategy's rolling 30-day Sharpe ratio. When a strategy's edge degrades by 30%+ for 30 consecutive days, it's automatically deprecated and removed from the active roster. Can restore if it recovers for 14 consecutive days.
