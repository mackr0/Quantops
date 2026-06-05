# 01 — Executive Summary

**Audience:** investors, executives, anyone evaluating QuantOpsAI without diving into the technical layers.
**Length:** ~3 pages.
**Last updated:** 2026-06-04 (audit reconciliation — see `docs/AUDIT_2026_06_04_DOC_RECONCILIATION.md`).

## What QuantOpsAI is

QuantOpsAI is an autonomous, AI-driven trading platform. It does what a discretionary equity portfolio manager and a quantitative research desk would do, end-to-end, on a single server: it sources candidates from a universe of US equities and ETFs, surfaces a high-fidelity feature picture for each candidate to a frontier-grade AI, captures the AI's decision and reasoning, watches the outcome resolve against price action, and feeds the result back into a self-improving learning stack.

Three claims define what makes the architecture distinctive:

1. **The AI is the portfolio manager, not a feature.** A single batched call to a large language model picks the trades per scan cycle from a ranked candidate list. The AI sees roughly fifty per-candidate signals plus full portfolio state, factor exposures, regime context, learned patterns from prior cycles, and per-stock track record. There is no rules engine downstream telling the AI "no" — there are only objective gates (crisis state, regulatory limits, margin checks) that block clearly unsafe actions.

2. **Every decision is captured, resolved, and used to retrain the system.** Each prediction is written to a journal with the full feature snapshot it was made on. When the prediction resolves (target hit, stop hit, time decay, exit signal), the row is labeled win/loss and pushed back into a two-layer meta-model (GBM batch + SGD freshness), a **two-layer calibrated specialist ensemble (179 deterministic rule-checkers + 8 LLM-narrative specialists)**, a self-tuning rule set (12 original layers + 5 deterministic guardrails added 2026-05-18), and an online (per-resolution) freshness layer. This is the proprietary asset — the corpus of resolved AI predictions in this exact decision context cannot be replicated by competitors.

3. **The platform runs 13 profiles in parallel inside three Alpaca paper accounts** via a virtual-account reconciliation layer. This is novel infrastructure: see "Virtual paper accounts," below. It compresses what would otherwise require 13 brokerage relationships into 3, and is the foundation for the rigorous baseline + ablation + scaling experiment in `docs/15_EXPERIMENT_DESIGN_2026_05_17.md`.

4. **The deterministic-vs-narrative split is the cost story.** Hundreds of zero-API-cost rule checkers handle structurally-checkable patterns (RSI overbought, insider clusters, gap into resistance, regulatory events, etc.) so the single batched LLM call only spends tokens on the synthesis work the rule layer structurally can't do. Steady-state observed AI spend across the 13-profile fleet: **~$0.27/day** at the current `gemini-2.5-flash-lite` rate. Quality goes up with specialist count; cost does not. The full enumeration of all 187 specialists with their individual roles is in `docs/24_SPECIALIST_CATALOG.md`.

## Why this might be valuable

The traditional case for AI-augmented trading rests on better signal extraction. QuantOpsAI's case rests on three additional structural advantages:

- **Compounding learning surface.** Every cycle generates labeled training data on the system's own predictions in the exact feature distribution it makes future predictions in. After 12 months of operation the meta-model has on the order of 50,000 labeled rows from the AI's own decisions. That asset cannot be bought.

- **Honest risk infrastructure.** The platform ships with the institutional risk machinery most retail systems skip: a Barra-style 21-factor risk model, parametric and Monte-Carlo Value-at-Risk and Expected Shortfall, seven historical stress scenarios (1987 → 2023 SVB), an active long-vol tail hedge, intraday risk auto-halts, drawdown-aware capital scaling, and a market-neutrality enforcement gate. These are described in "Risk Architecture" below and enumerated exhaustively in `docs/08_RISK_CONTROLS.md`.

- **Capital-efficient testing.** The virtual-account architecture means the cost of testing a new strategy variant is one settings toggle and one new SQLite database — not a new brokerage account, not new capital, not new ops overhead.

## What it actually trades

The platform trades long and short positions in US equities, options on those equities (single-leg and multi-leg structures), and statistical-arbitrage pairs. Crypto infrastructure (segment + per-profile flag `enable_crypto`) is wired but `enable_crypto=0` on every profile today — a deliberate baseline-control choice (per `project_capital_allocation` memory rationale). Futures and FX are scoped as future work via IBKR in `OPEN_ITEMS.md` §4a.

Concretely:

- **25 plugin equity strategies**, both bullish (gap reversal, news sentiment spike, short squeeze setup, earnings drift, insider cluster, fifty-two-week breakout, MACD cross confirmation, sector momentum rotation, analyst upgrade drift, short-term reversal, volume dryup breakout, max-pain pinning — 12 total) and bearish (breakdown of support, distribution at highs, failed breakout, parabolic exhaustion, relative weakness in strong sector, relative weakness universe-wide, earnings disaster short, catalyst filing short, sector rotation short, IV regime short, insider selling cluster, high IV-rank fade, vol regime — 13 total). Canonical registry: `strategies/__init__.py`.

- **Five single-leg options primitives** (long call, long put, covered call, cash-secured put, protective put) plus **eleven multi-leg primitives** (four vertical spreads — bull call, bear put, bull put, bear call — plus iron condor, iron butterfly, long straddle, short straddle, long strangle, calendar spread, diagonal spread).

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

1. **Test discipline.** 4,561 tests pass (1 skipped — an `_EMPTY_FIRE_EXEMPT` rule whose purpose IS to fire on minimal context). Test skips were systematically removed; new skips are blocked at code review.

2. **Anti-drift guardrails.** Static-analysis tests prevent the failure modes that retail-trading systems silently suffer from: hidden levers (every per-profile scheduled feature must have a settings toggle); meta-features without UI surfaces; snake_case identifiers leaking into rendered HTML; columns added to the schema that aren't either auto-tuned or explicitly enumerated as user-set; new modules that ship without changelog entries.

3. **Honest-limits convention.** Every module that ships with a documented approximation prefixes the approximation with the marker `**Honest limits:**` in its docstring. This is not aspirational — it is enforced by code review and surfaced in `OPEN_ITEMS.md` §11.

## Where to go next

- **For the AI methodology**: `docs/02_AI_SYSTEM.md`.
- **For trading strategy specifics**: `docs/03_TRADING_STRATEGY.md`.
- **For the system's epistemic stance and how decisions are made**: `docs/10_METHODOLOGY.md`.
- **For everything still pending**: `OPEN_ITEMS.md`.
