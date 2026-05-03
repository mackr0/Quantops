# 12 — Scaling and Graduation

**Audience:** operators planning capital deployment beyond the current $10K paper baseline.
**Purpose:** what changes at $10K paper → $10K real → $50K → $250K → $1M+. What breaks. What doesn't.
**Last updated:** 2026-05-03.

## 0. Where this stands today

- **Mode:** paper trading on Alpaca.
- **Capital:** simulated $10K per profile (configurable per virtual account).
- **Profiles:** 10+, sharing 3 paper accounts via the virtual-account architecture.
- **Goal of current stage:** prove the AI's prediction signal generates positive risk-adjusted returns over a meaningful sample. Two weeks of decision data is the corpus today; 30+ trading days with consistent positive P&L is the trigger to consider Stage 2.

## 1. Stages

### Stage 1 — $10K Paper (CURRENT)

**Purpose:** prove the strategy stack works in simulation.

**Success criteria:**

- ≥ 30 trading days of consistent operation.
- Positive aggregate P&L across all profiles.
- ≥ 45% scratch-excluded win rate.
- Meta-model AUC ≥ 0.55 on at least 3 profiles.
- Specialist Platt-scaling layers fitted with ≥ 100 samples each.

**What's running:**

- Full AI pipeline.
- Slippage model calibrated from paper fills (with the documented limit that real fills will deviate).
- All risk controls active.
- Self-tuner adjusting parameters nightly.

**What to watch:**

- AI prediction accuracy via the AI Brain tab.
- Specialist veto rates and calibration drift.
- Strategy alpha decay.
- Slippage model drift on the calibration panel.

### Stage 2 — $10K Real Money

**Purpose:** validate the strategy stack with real fills + real fees.

**Prerequisites:**
- Stage 1 success criteria met.
- Operator has reviewed every risk control documented in `docs/08_RISK_CONTROLS.md`.
- Operator accepts up to $1,000 (10%) loss as tuition before pausing.

**Changes required:**

| Change | Reason |
|---|---|
| Switch Alpaca paper → live account | Live execution. |
| Slippage K recalibration after 30 days | Paper-fitted K will deviate. |
| Tighter drawdown thresholds (e.g. `drawdown_pause_pct: 0.20 → 0.10`) | Real-money loss tolerance is lower. |
| Start with **one** market type (the best paper performer) | Concentrate risk on validated signal. |
| Real-time P&L monitoring + alerts | Off-platform alerting (Healthchecks.io, Sentry, etc). |
| Manual review of every trade for the first 5-10 trades | Sanity check. |

**What stays the same:** the entire codebase. The Alpaca adapter accepts a live account token in place of a paper one. No code changes needed.

**Risk budget:** $1,000 loss. If hit, pause and analyze. If not hit and 60+ days of positive Sharpe, advance to Stage 3.

### Stage 3 — $50K Real Money

**Purpose:** validate at a size where slippage and liquidity become material.

**Prerequisites:**
- Stage 2 profitable over 60+ trading days.

**Changes required:**

| Change | Reason |
|---|---|
| **Polygon real-time data subscription (~$50/mo)** | yfinance/Alpaca-free latency becomes a problem. |
| **Min dollar-volume filter: $5M+ daily** | Below this, our orders are >0.1% of ADV → significant slippage. |
| **Limit orders by default** | Reduce slippage on illiquid names. Set `use_limit_orders=1`. |
| **Tighter correlation limit:** `max_correlation: 0.7 → 0.5` | Correlated losses scale faster at size. |
| **Tighter sector concentration:** `max_sector_positions: 5 → 3` | Same. |
| **Add VaR-based pre-trade gate** (currently informational) | Block entries that would push 95% VaR > 5% of book. |
| **Drop `microsmall` profile** | Sub-$5 names lack liquidity at $50K notional. |

**What stays the same:**

- AI pipeline, specialist ensemble, meta-model, self-tuner.
- Virtual-account architecture (now with 3 LIVE accounts; same reconciliation).

### Stage 4 — $100K-$250K

**Purpose:** validate at a size where intraday execution timing matters.

**Prerequisites:**
- Stage 3 profitable over 90+ trading days.

**Changes required:**

| Change | Reason |
|---|---|
| **WebSocket streaming architecture** (replaces 15-min polling) | 5-15 min cycle starts missing intraday opportunities. |
| **Level 2 order book analysis** | Bid/ask imbalance becomes a meaningful signal. |
| **Min dollar-volume floor: $10M+ daily** | Same scaling logic. |
| **VWAP order execution** | Spread orders across time to reduce impact. |
| **Iceberg orders** | Hide order size from other traders. |
| **Portfolio-level VaR pre-trade** (already informational; now hard gate) | Block entries breaching `max_var_95_pct_of_book`. |
| **Tax-lot tracking** | Tax optimization becomes worth the complexity. |
| **PDT rule monitoring** | Pattern Day Trader rules apply. |

**What changes architecturally:**

- New `streaming.py` module using `alpaca_trade_api.Stream`.
- Trade pipeline becomes event-driven: AI re-evaluates on significant price/volume events, not on a fixed schedule.
- The 5-15 minute cycle becomes the FALLBACK; primary signal flow is real-time streaming.

### Stage 5 — $1M+

**Purpose:** professional-grade execution.

**Prerequisites:**
- Stage 4 profitable over 180+ trading days.

**Changes required:**

| Change | Reason |
|---|---|
| **Event-driven architecture (full rewrite of execution layer)** | Sub-second execution required. |
| **Smart order routing (split large orders)** | Single venue exhaustion. |
| **Market impact modeling** (estimate price impact pre-trade) | Already implemented (slippage model); now used as hard gate. |
| **Drop `smallcap` profile** | Insufficient liquidity. |
| **Mid + large cap only** | Capacity. |
| **Position size capped at 1% of stock's daily $vol** | Hard cap. |
| **Real-time portfolio VaR** | Continuous, not daily snapshot. |
| **Sector exposure limits: max 20% per sector** | Tighter than current 30%. |
| **Correlation matrix updated daily** | Stale matrices fail at this size. |
| **Maximum drawdown triggers reviewed with actual capital at risk** | Real money, real consequences. |
| **Dedicated server (not shared $6 droplet)** | Reliability + redundancy. |
| **Redundant connections to exchange** | Single-point-of-failure risk. |
| **Monitoring + alerting for system failures** | 24/7 ops. |
| **Pattern day trader compliance monitoring** | Automated. |
| **Short selling borrowing cost integration** | Currently approximated; now real broker rates. |
| **Potential SEC reporting** | 13F at $100M+. |

## 2. What scales WITHOUT changes

These components work at any account size:

| Component | Why |
|---|---|
| AI analysis pipeline | Same prompt, same analysis, same per-cycle cost. |
| Self-tuner | Performance tracking is percentage-based. |
| Strategy engines | Signal generation is price/volume ratio based. |
| Meta-model | Trains on relative outcomes. |
| Specialist ensemble | Reasoning surface is independent of size. |
| Risk controls (most) | Percentage-based. |
| Virtual-account architecture | Designed to scale to N profiles. |
| Web UI / dashboards | Display layer is size-agnostic. |

## 3. What breaks at scale (table)

| Component | Breaks at | Reason | Fix |
|---|---|---|---|
| Market orders | $50K+ | Slippage on small caps | Limit/VWAP orders |
| Microcap universe | $50K+ | Orders >1% of daily volume | Drop microcaps |
| Smallcap universe | $250K+ | Same liquidity issue | Raise volume floors |
| 5-15 min cycle | $100K+ | Too slow, miss opportunities | WebSocket streaming |
| yfinance + Alpaca-free | $50K+ | Delayed prices increase slippage | Polygon subscription |
| `max_correlation = 0.7` | $100K+ | Correlated losses too large | 0.5 |
| Fixed % position sizing | $1M+ | % of equity exceeds % of daily volume | Volume-based sizing |
| Polling-based scheduler | $1M+ | Latency unacceptable | Event-driven |
| Shared $6 droplet | $100K+ | Single point of failure | Dedicated infra |

## 4. Decision framework: when to graduate

The platform's preference is conservative graduation. Move forward only when ALL of:

- The current stage's success criteria are met.
- The operator has reviewed the new stage's risk-controls implications.
- The operator has the capital to absorb the documented risk budget.
- The infrastructure changes for the new stage are complete BEFORE capital is added.

**Do not** graduate just because the current stage feels stable; the proper trigger is a sustained track record (60-180 days depending on stage).

**Do** graduate when the data justifies it. The cost of running the current stage is low; the cost of premature graduation can be material.

## 5. Timeline estimate (illustrative, not promised)

| Stage | Capital | Earliest possible | Realistic |
|---|---|---|---|
| 1: Paper | $10K simulated | Now | Now |
| 2: $10K Real | $10K | T+30d | T+60-90d |
| 3: $50K Real | $50K | T+90d | T+180d |
| 4: $100K-$250K | $250K | T+180d | T+360d |
| 5: $1M+ | $1M+ | T+360d | T+540d+ |

## 6. Costs per stage

| Stage | Monthly cost (data + infra) | Monthly AI cost (current rate) |
|---|---|---|
| 1: Paper | $6 (droplet) | $30-60 |
| 2: $10K Real | $6 (droplet) | $30-60 |
| 3: $50K Real | $56 (+ Polygon $50) | $30-60 |
| 4: $100K-$250K | ~$150-300 (Polygon + dedicated infra) | $50-100 |
| 5: $1M+ | $500-1000+ (dedicated infra + monitoring) | $100-200 |

AI cost grows sub-linearly because more capital doesn't mean more cycles — it means larger orders per cycle.

## 7. Multi-profile scaling

The virtual-account architecture means N profiles share 3 Alpaca accounts. Scaling from 10 profiles to 20+ is a configuration change, not a code change. Constraints:

- Each Alpaca account has a position-count limit (~200 open positions, but margin requirements bite earlier).
- The cross-account reconciler audits sum-of-virtual = broker-actual; drift indicates someone touched the broker outside the platform.
- Per-cycle wall-clock time scales linearly with profiles. At 20+ profiles, single-process cycle latency may need parallelization (multi-process scheduler).

## 8. What's NOT in the roadmap

These are explicitly out of scope:

- **Latency arbitrage.** Sub-microsecond + co-location requirements.
- **Market making.** Exchange membership + low-latency infra.
- **Block trading.** Capital + relationships.
- **Index inclusion arbitrage.** Capacity to move millions in seconds.
- **Insider expert networks.** Paid services not aligned with the proprietary-data-asset model.

These are real differentiators of billion-dollar funds but the gap is structural, not addressable in this codebase.

## 9. Cross-asset graduation

QuantOpsAI today trades US equities + options + cointegrated equity pairs only. Graduation to:

- **Futures (commodities, rates, FX):** requires `4a Futures + FX via IBKR` from `OPEN_ITEMS.md`. ~1 month of build.
- **Crypto:** Alpaca's crypto endpoint is wired; `crypto` market type exists. Currently unused; awaiting strategy thesis.
- **Foreign equities:** out of scope (Alpaca is US-only).

## 10. Operating discipline as you scale

The methodology principles in `docs/10_METHODOLOGY.md` become MORE important, not less, as capital grows:

- **Test discipline.** A bug that costs $100 at $10K is the same proportional cost as a bug costing $10,000 at $1M. Same engineering rigor.
- **No hidden levers.** Auditability matters more.
- **Honest limits.** A documented coverage gap that you'd live with at $10K may be unacceptable at $1M.
- **Forward-only.** Backfilling features into historical data to "see how it would have done" is more tempting at higher capital. Resist; the deception only hurts you.

## 11. Reference

- `docs/01_EXECUTIVE_SUMMARY.md` — what the system is.
- `docs/04_TECHNICAL_REFERENCE.md` — what would need to change architecturally.
- `docs/07_OPERATIONS.md` — current ops baseline.
- `docs/08_RISK_CONTROLS.md` — risk infrastructure.
- `OPEN_ITEMS.md` — including SCALING-related open items.
