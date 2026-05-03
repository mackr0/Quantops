# 01 — Executive Summary

**Audience:** investors, executives, anyone evaluating QuantOpsAI without diving into the technical layers.
**Length:** ~3 pages.
**Last updated:** 2026-05-03.

## What QuantOpsAI is

QuantOpsAI is an autonomous, AI-driven trading platform. It does what a discretionary equity portfolio manager and a quantitative research desk would do, end-to-end, on a single server: it sources candidates from a universe of US equities and ETFs, surfaces a high-fidelity feature picture for each candidate to a frontier-grade AI, captures the AI's decision and reasoning, watches the outcome resolve against price action, and feeds the result back into a self-improving learning stack.

Three claims define what makes the architecture distinctive:

1. **The AI is the portfolio manager, not a feature.** A single batched call to a large language model picks zero-to-three trades per scan cycle from a ranked candidate list. The AI sees roughly fifty per-candidate signals plus full portfolio state, factor exposures, regime context, learned patterns from prior cycles, and per-stock track record. There is no rules engine downstream telling the AI "no" — there are only objective gates (crisis state, regulatory limits, margin checks) that block clearly unsafe actions.

2. **Every decision is captured, resolved, and used to retrain the system.** Each prediction is written to a journal with the full feature snapshot it was made on. When the prediction resolves (target hit, stop hit, time decay, exit signal), the row is labeled win/loss and pushed back into a two-layer meta-model, a five-specialist calibrated ensemble, a twelve-layer self-tuning rule set, and an online (per-resolution) freshness layer. This is the proprietary asset — the corpus of resolved AI predictions in this exact decision context cannot be replicated by competitors.

3. **The platform tests ten or more strategies in parallel inside three free Alpaca paper accounts.** This is novel infrastructure: see "Virtual paper accounts," below. It compresses what would otherwise require ten brokerage relationships into three.

## Why this might be valuable

The traditional case for AI-augmented trading rests on better signal extraction. QuantOpsAI's case rests on three additional structural advantages:

- **Compounding learning surface.** Every cycle generates labeled training data on the system's own predictions in the exact feature distribution it makes future predictions in. After 12 months of operation the meta-model has on the order of 50,000 labeled rows from the AI's own decisions. That asset cannot be bought.

- **Honest risk infrastructure.** The platform ships with the institutional risk machinery most retail systems skip: a Barra-style 21-factor risk model, parametric and Monte-Carlo Value-at-Risk and Expected Shortfall, seven historical stress scenarios (1987 → 2023 SVB), an active long-vol tail hedge, intraday risk auto-halts, drawdown-aware capital scaling, and a market-neutrality enforcement gate. These are described in "Risk Architecture" below and enumerated exhaustively in `docs/08_RISK_CONTROLS.md`.

- **Capital-efficient testing.** The virtual-account architecture means the cost of testing a new strategy variant is one settings toggle and one new SQLite database — not a new brokerage account, not new capital, not new ops overhead.

## What it actually trades

The platform trades long and short positions in US equities, options on those equities (single-leg and multi-leg structures), and statistical-arbitrage pairs. It does not trade futures, FX, or crypto in production yet — those are scoped as future work in `OPEN_ITEMS.md`.

Concretely:

- **20+ equity strategies**, both bullish (momentum breakout, volume spike, mean reversion, gap and go, insider cluster, news sentiment spike, earnings drift, short squeeze setup, fifty-two-week breakout, MACD cross confirmation, sector momentum rotation, analyst upgrade drift, short-term reversal, volume dryup breakout, fifty-two-week breakout) and bearish (breakdown of support, distribution at highs, failed breakout, parabolic exhaustion, relative weakness in strong sector, earnings disaster short, catalyst filing short, sector rotation short, IV regime short, relative weakness universe-wide).

- **Eleven options strategy primitives**: long call, long put, covered call, cash-secured put, protective put, four vertical spreads (bull call, bear put, bull put, bear call), iron condor, iron butterfly, long and short straddle, long strangle, calendar spread, diagonal spread.

- **Cointegration-driven pair book** with weekly Engle-Granger universe scan, daily pair retest, and Z-score-based entry/exit/stop signals.

- **Active long-vol portfolio hedge** (off by default, opt-in): SPY puts that automatically open when drawdown ≥ 5%, crisis state ≥ "elevated," or projected 95% VaR ≥ 3% of book.

The trading philosophy and per-strategy details are in `docs/03_TRADING_STRATEGY.md`.

## Virtual paper accounts (the testing-cost advantage)

Alpaca permits three paper trading accounts per user. QuantOpsAI extends this to **ten or more independent strategies** by virtualizing profiles within each Alpaca account.

How it works:

- Each profile (e.g. "Mid-Cap Momentum," "Small-Cap Shorts," "Crypto Mean Reversion") has its own SQLite database, initial capital figure, position book, and outcome ledger.
- A profile maps to one of the three real Alpaca paper accounts.
- The system maintains a per-profile virtual position book derived from the trades table via FIFO accounting. Trade decisions flow through the per-profile pipeline; the broker call goes to the shared Alpaca account.
- A daily `_task_cross_account_reconcile` task verifies that the sum of virtual positions across profiles sharing a real account matches the broker's reported holdings. Any drift triggers an alert.
- Per-profile virtual P&L is computed by attributing each fill back to the virtual order that initiated it.

This means a single user can run a market-neutral long/short strategy, a small-cap shorts strategy, a wheel-options strategy, and a stat-arb pair book simultaneously in parallel — each with its own capital, its own meta-model, its own slippage calibration, its own learned patterns — at zero incremental brokerage cost. **The same architecture, when rolled forward to live trading, is the foundation for running multiple capital pools with isolated P&L attribution and isolated risk budgets.**

The detailed architecture is in `docs/04_TECHNICAL_REFERENCE.md` under "Virtual Accounts."

## Risk architecture

The platform layers six independent risk controls. Any one of them can block a trade or reduce position size; they are not cumulative, they are each sufficient.

| Layer | What it does | When it fires |
|---|---|---|
| **Crisis state monitor** | Cuts new long entries; scales position sizes 1.0× → 0.25× | Cross-asset stress signals (VIX term, SPY/TLT/GLD correlation breaks, credit spreads, gold safe-haven rallies, price-shock clusters). |
| **Intraday risk halt** | Blocks new entries until 60-minute auto-clear | Today's drawdown ≥ 2× 7-day average, SPY hourly vol ≥ 3× 20-day average, sector swing ≥ 3%, held-position trading halt. |
| **Per-trade stops** | Broker-managed protective orders | Every entry receives a trailing stop or static stop loss at the broker. |
| **Portfolio risk model** | Daily Barra-style snapshot in the AI prompt | 21-factor exposures, parametric and Monte Carlo VaR / ES, seven historical stress scenarios. |
| **Long-vol tail hedge** | Active SPY puts | When drawdown / crisis / VaR triggers fire (off by default — opt-in). |
| **Balance and neutrality gates** | Hard block on entries that worsen long/short balance or book beta | Long/short profiles only; user-set targets. |

The full enumeration, including every kill switch and validation gate in the trade pipeline, is in `docs/08_RISK_CONTROLS.md`.

## What's honest about the limits

QuantOpsAI is a paper-trading platform with two weeks of accumulated decision data as of this writing. The following caveats apply and should not be obscured:

- **Slippage calibration is paper-fitted.** The slippage model's K coefficient is calibrated from paper fills. Real-money fills will deviate. The model assumes IID slippage per trade; correlated regimes (full days of wide spreads) are a documented limit, with by-day bootstrap mode available as a partial mitigation.

- **Synthetic options backtester is approximate.** Historical options pricing uses Black-Scholes with trailing-30-day realized volatility as an IV proxy. It captures direction and approximate magnitude; it does not capture bid-ask spread, IV term structure, or catalyst vol expansion. It is sufficient for strategy validation, not precise P&L forecasting.

- **Stress scenarios miss cross-asset risk.** The factor set includes equity sectors, equity styles, and Ken French factors; rates, FX, and commodities are not yet in the model. A 2022-style rate shock under-reports.

- **Two weeks of data is a small calibration corpus.** Meta-model AUC, specialist Platt-scaling fits, slippage K, and learned patterns will all materially improve with more resolved predictions. The system is wired to compound this asset over time, but interpreting current performance requires acknowledging the small sample.

- **Latency arbitrage, market making, block trading, and index-inclusion arbitrage are out of scope.** These are billion-dollar-fund differentiators that are structural, not addressable in software at this scale.

The full open-items list, including paid-data upgrades that would close specific gaps, is in `OPEN_ITEMS.md`.

## Engineering quality signals

Three things distinguish this codebase from typical retail-trading projects:

1. **Test discipline.** 1,914 tests pass, zero skipped. Test skips were systematically removed; new skips are blocked at code review.

2. **Anti-drift guardrails.** Static-analysis tests prevent the failure modes that retail-trading systems silently suffer from: hidden levers (every per-profile scheduled feature must have a settings toggle); meta-features without UI surfaces; snake_case identifiers leaking into rendered HTML; columns added to the schema that aren't either auto-tuned or explicitly enumerated as user-set; new modules that ship without changelog entries.

3. **Honest-limits convention.** Every module that ships with a documented approximation prefixes the approximation with the marker `**Honest limits:**` in its docstring. This is not aspirational — it is enforced by code review and surfaced in `OPEN_ITEMS.md` §11.

## Where to go next

- **For the AI methodology**: `docs/02_AI_SYSTEM.md`.
- **For trading strategy specifics**: `docs/03_TRADING_STRATEGY.md`.
- **For the system's epistemic stance and how decisions are made**: `docs/10_METHODOLOGY.md`.
- **For everything still pending**: `OPEN_ITEMS.md`.
