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

## Self-Learning Loop — 9 Layers of Autonomy

The AI isn't static. The system continuously adjusts how it trades based on its own performance. The architecture is organized into 9 layers, each addressing a different decision surface. See `AUTONOMOUS_TUNING_PLAN.md` for the full design and `SELF_TUNING.md` for live status.

The layers below the line are designed and rolling out wave by wave; the ones above the line are active in production today.

```
┌────────────────────────────────────────────────────────────┐
│                     COST GUARD (cross-cutting)              │
│   Every spend-affecting autonomous action checks the daily  │
│   API ceiling. Over-budget changes surface as recommendations│
│   with cost estimates, not silent debits.                    │
└────────────────────────────────────────────────────────────┘
       │
       ▼
LAYER 1 — Parameter coverage
LAYER 2 — Weighted signal intensity     ─── ACTIVE OR ROLLING OUT ───
LAYER 3 — Per-regime overrides
─────────────────────────────────────────────────────────────
LAYER 4 — Per-time-of-day overrides
LAYER 5 — Cross-profile insight sharing
LAYER 6 — Adaptive AI prompt structure
LAYER 7 — Per-symbol overrides
LAYER 8 — Self-commissioned new strategies
LAYER 9 — Automatic capital allocation (opt-in)
```

### Layer 1 — Parameter Coverage (active)

Every numeric and binary parameter in `trading_profiles` either has a tuning rule that adjusts it based on observed performance, or is on a tight `MANUAL_PARAMETERS` allowlist enforced by an anti-regression test. Manual entries are limited to: AI provider/model (cost-sensitive strategic choice — opt-in toggle planned), consensus configuration (architectural), schedule (user lifestyle), secrets, identity, historical baselines.

Today the tuner autonomously manages **~23 parameters** including:
- Confidence threshold, position sizing, stop/take-profit (fixed and ATR-based)
- Concentration limits (max positions, max correlation, max sector positions)
- Drawdown thresholds (pause and reduce)
- Entry filters (volume, momentum, gap, RSI bands, price band)
- Timing (earnings avoidance, opening-minute skip)
- Strategy toggles (4 legacy + auto-deprecation of all 16+ modular strategies via alpha_decay)
- Short-selling enable/disable (defensive auto-disable on persistent losses)
- MAGA mode (auto-disable when political context underperforms)

Every adjustment respects: bound clamping (`param_bounds.PARAM_BOUNDS`), 3-day per-parameter cooldown, automatic reversal if the change worsens performance, and an explicit history check.

### Layer 2 — Weighted Signal Intensity (designed)

Every signal the AI sees gets a per-profile weight on a 4-step ladder (`1.0 → 0.7 → 0.4 → 0.0`). Weight `1.0` is full strength; `0.0` omits the signal entirely from the prompt. Intermediate weights inject a discount hint into the prompt so the AI knows the signal has been historically weak for this profile.

This generalizes binary toggles. Insider buying cluster might be signal-positive overall but unreliable for a specific profile — instead of deleting it, drop its weight to 0.4 so the AI keeps the information but doesn't overweight it. Strategy toggles, alt-data signals, and even short-selling intensity all roll into this system.

### Layer 3 — Per-Regime Overrides (designed)

Each parameter can have regime-specific values: `bull`, `bear`, `sideways`, `volatile`, `crisis`. At decision time, `resolve_param(profile, name, regime)` returns the regime-specific value if it exists with sufficient sample size, else falls back to global. The tuner detects when a parameter performs differently per regime and creates per-regime overrides automatically.

### Layer 4 — Per-Time-of-Day Overrides (designed)

Same idea, bucketed by intraday window: open (09:30–10:30), midday (10:30–14:30), close (14:30–16:00). Different behaviors at the open vs close are well-documented in equities; the tuner learns which parameters are time-sensitive.

### Layer 5 — Cross-Profile Insight Sharing (designed)

When one profile makes a successful adjustment, the same detection rule runs against every other enabled profile's own data. Profiles with the same pattern apply the same change (their own data triggers it; no value-copying). The fleet learns ~10× faster than profiles in isolation, with no extra API cost.

### Layer 6 — Adaptive AI Prompt Structure (designed)

The prompt's structure — section order, section verbosity, signal grouping — is itself a tunable surface. The tuner runs implicit A/B tests across cycles and reinforces structures correlated with higher win rates. Cost-gated so it doesn't drift toward longer prompts.

### Layer 7 — Per-Symbol Overrides (designed)

Some symbols behave differently (NVDA's optimal stop-loss is not KO's). For symbols with ≥20 individual resolved predictions, the tuner can create per-symbol parameter overrides. Most fine-grained layer; longer cooldown (7 days) to prevent over-fitting on small symbol samples.

### Layer 8 — Self-Commissioned New Strategies (designed)

Builds on Phase 7 (auto-strategy generation). The tuner identifies *gaps* in current strategy coverage — patterns where the existing strategies didn't fire but the AI made correct calls anyway, representing untapped edges. It triggers the strategy generator with a focused brief, gated by cost.

### Layer 9 — Automatic Capital Allocation (designed, opt-in)

Across profiles, capital flows toward proven edge. Weekly rebalance computes a `capital_score = recent_sharpe × (1 + win_rate)` and reallocates within ±25%/+200% bounds per rebalance. Opt-in by default — flipped to auto-action when `auto_capital_allocation` is enabled, similar to `ai_model_auto_tune`.

### Cost Guard (cross-cutting)

`cost_guard.py` (designed) wraps every cost-affecting autonomous action. The tuner respects a per-user daily API spend ceiling. Cost-exceeding changes surface as the only legitimate "Recommendation:" — with explicit cost estimates — for human approval. This is the single carve-out that the anti-recommendation-only guardrail allows.

---

## Meta-Model (operates alongside the autonomy layers)

A gradient-boosted classifier (`meta_model.py`) trained daily on the AI's own resolved predictions. Learns "given everything the main AI saw at decision time, was the AI right?" and re-weights the AI's confidence at decision time based on the historical pattern. Needs 100+ resolved predictions to start; AUC is reported in the activity feed (e.g., "AUC 0.83" means strong signal separating right from wrong calls).

The meta-model and the self-tuner complement each other:
- Meta-model: per-prediction probability adjustment.
- Self-tuner: per-parameter, per-signal, per-regime adjustment.

Both learn from the same resolved-prediction stream.

---

## Alpha Decay Monitor (continuous)

`alpha_decay.py` tracks each strategy's rolling 30-day Sharpe vs lifetime baseline. Auto-deprecates strategies whose rolling Sharpe degrades for 30+ consecutive days; auto-restores when rolling Sharpe recovers for 14+ consecutive days. The self-tuner now also feeds into this pipeline — when it identifies a non-toggleable strategy underperforming, it calls `deprecate_strategy()` directly instead of waiting for the rolling-Sharpe trigger.

Manual override via the **Restore** button on the AI Intelligence → Strategy tab.

---

## What This Means in Practice

The end state of the autonomy layers is that humans set **strategic** direction (which AI provider, what daily cost ceiling, which markets to trade, what schedule) and the system handles every **tactical** decision: which signals to weight high or low, what stop-loss makes sense in volatile vs bull regimes, which sector cap fits today's correlation pattern, when to deprecate a strategy that's lost its edge, when to spin up a new one to fill a coverage gap, and how to allocate capital across profiles.

The whole point is that the system makes better, faster, smarter decisions than a person can — and learns from every one of them.
