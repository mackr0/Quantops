# 09 — Glossary

**Audience:** cross-audience reference. Any term used in the technical docs without definition is defined here.
**Last updated:** 2026-05-03.

Alphabetical. Linked terms reference other glossary entries.

---

**ADV** — *Average Daily Volume*. The 20-day rolling average of trading volume in shares. Used in slippage modeling: order size as a fraction of ADV is the [participation rate](#participation-rate). On `trades.adv_at_decision`.

**Alpha decay** — The phenomenon where a strategy's edge degrades over time as the market adapts to the strategy's pattern, or as the underlying market regime shifts. Measured in QuantOpsAI by rolling 30-day [Sharpe ratio](#sharpe-ratio) vs lifetime baseline. See `alpha_decay.py`.

**Almgren-Chriss** — Square-root market-impact model. The expected adverse price impact of an order is approximately `K × √participation_rate`, where K is calibrated empirically. Used in QuantOpsAI's slippage model.

**Backwardation** — When near-dated futures (or option implied volatility for nearer expiries) are priced higher than further-dated. Indicates immediate event/uncertainty pricing. Inverse of [contango](#contango).

**Barra-style risk model** — A multi-factor portfolio risk model. Decomposes a portfolio's variance into factor variance (systematic) plus idiosyncratic variance (name-specific). The "Barra" name comes from the original commercial product (Barra Inc.). QuantOpsAI uses a 21-factor variant.

**Beta** — Measure of a stock's (or portfolio's) sensitivity to market movements. Beta = 1 means the asset moves with the market 1-for-1. Beta = 0 means uncorrelated. Negative beta means inversely correlated. The portfolio's gross-weighted beta is the system's `current_book_beta`.

**Black-Scholes** — Foundational option pricing model. Inputs: spot price, strike, time-to-expiry, risk-free rate, volatility. Output: theoretical option price. QuantOpsAI uses Black-Scholes for synthetic backtest pricing and for [implied volatility](#implied-volatility) inversion.

**Bootstrap** — Statistical resampling technique. Drawing samples (with replacement) from an empirical distribution to estimate uncertainty. Used in QuantOpsAI's slippage model and Monte Carlo backtester.

**Bracket order** — Broker order with attached protective stop and/or take-profit orders that get submitted alongside the main order. QuantOpsAI uses Alpaca's bracket-order machinery via `bracket_orders.py`.

**Break-even close** — A trade that exited near its entry price (|pnl_pct| < 0.5%). QuantOpsAI's "scratch" classification — excluded from win rate counts since it's neither a real win nor a real loss.

**Calibrator (Platt scaling)** — Logistic regression layer fitted to map raw model output (e.g. specialist confidence 0-100) to empirical P(correct). Each specialist in QuantOpsAI's ensemble has its own Platt calibrator.

**Catalyst** — Event expected to drive a stock's price (earnings, FDA approval, M&A announcement, SEC filing). QuantOpsAI tracks catalysts via earnings calendar, [PDUFA](#pdufa) dates, and SEC filing alerts.

**Cointegration** — A statistical relationship between two non-stationary time series whose linear combination IS stationary. Foundation of pair trading: when the spread between two cointegrated assets diverges, it tends to revert. Tested via the [Engle-Granger](#engle-granger) procedure.

**Contango** — When far-dated futures (or option IV for further expiries) are priced higher than near-dated. Inverse of [backwardation](#backwardation). Calendar spreads typically benefit from contango stability.

**Cost guard** — QuantOpsAI's daily AI-spend ceiling enforcement. Cross-cuts all autonomous actions; over-budget changes are surfaced as recommendations rather than auto-applied.

**Covered call** — Options strategy: long stock + short call at higher strike. Generates premium income; caps upside.

**Crisis state** — QuantOpsAI's cross-asset risk state machine. Levels: normal / elevated / crisis / severe. Affects new-entry permission and position sizing.

**Decision price** — The price at which a strategy or AI made its decision (typically the snapshot price at the start of the cycle). Compared to actual fill price to compute realized [slippage](#slippage).

**Delta** — First derivative of an option's price with respect to the underlying. Approximately the probability the option finishes in-the-money. A delta of 0.30 means the option moves about 30 cents for every dollar move in the underlying. The system's `options_delta_hedger.py` uses delta to keep long-vol option positions stock-side neutral.

**Drift (calibration drift)** — When predicted vs realized deviates over time, indicating the model's parameters have grown stale. QuantOpsAI's slippage history panel monitors mean delta between predicted_slippage_bps and realized.

**Engle-Granger** — Statistical test for [cointegration](#cointegration). Two-step: (1) regress one series on the other, (2) test residuals for stationarity via [ADF](#adf-test). QuantOpsAI uses statsmodels' implementation.

**Expected Shortfall (ES)** — Average loss conditional on the loss exceeding the [VaR](#value-at-risk-var) threshold. ES_95 = average loss given the loss is in the worst 5% of outcomes. More tail-aware than VaR alone; required by Basel III for banks.

**Factor** — A common driver of returns. Examples: market (Mkt-RF), size (SMB), value (HML), momentum (Mom), quality, low-vol. QuantOpsAI's portfolio risk model uses 21 factors.

**FIFO accounting** — First-in-first-out matching of buy and sell orders for P&L attribution. QuantOpsAI's virtual position book is computed via FIFO from the trades table.

**Fractional Kelly** — A modification of the Kelly criterion that uses a fraction (commonly 1/4 or 1/2) of the full Kelly bet size. Quarter Kelly is QuantOpsAI's default — full Kelly is too aggressive given parameter estimation uncertainty.

**Gamma** — Second derivative of an option's price (i.e., the rate of change of [delta](#delta)). High gamma means delta changes rapidly. ATM short-dated options have the highest gamma.

**GBM** — *Gradient Boosting Machine*. Tree-based ensemble model that builds trees sequentially, each correcting errors of the prior. QuantOpsAI's batch [meta-model](#meta-model) is a scikit-learn GradientBoostingClassifier.

**GEX** — *Gamma Exposure*. The aggregate dollar gamma of options outstanding on an underlying, signed by hedger position. Positive GEX → market makers buy on dips, sell on rallies (stabilizing). Negative GEX → opposite (destabilizing).

**Half-spread** — Half of the bid-ask spread. The deterministic component of [slippage](#slippage) — the price impact of trading at the touch.

**HTB** — *Hard-to-borrow*. Stocks with limited shares available for shorting; lenders charge higher borrow rates (sometimes 5-50%+ annualized). QuantOpsAI applies an asymmetric size penalty to HTB shorts.

**Idiosyncratic risk** — Stock-specific variance not explained by common factors. The "residual" after factor regression. Diversifies away across positions, factor risk doesn't.

**Implied volatility (IV)** — The volatility input to [Black-Scholes](#black-scholes) that produces the observed option price. The market's forecast of future volatility. IV rank = current IV's percentile in its 1-year history.

**Iron condor** — Multi-leg options strategy: short OTM put + long further-OTM put + short OTM call + long further-OTM call. Profits when the underlying stays between the short strikes. Defined risk.

**K (slippage K)** — The market-impact coefficient in `bps = K × √participation_rate`. Calibrated empirically per market_type from `trades.fill_price - decision_price` pairs.

**Kelly criterion** — Optimal-bet-sizing formula: `f* = (bp - q) / b`, where p = win rate, q = 1-p, b = avg_win / avg_loss. Maximizes long-run geometric growth. See [fractional Kelly](#fractional-kelly).

**Ledoit-Wolf** — A covariance-matrix shrinkage estimator that interpolates between the sample covariance and a diagonal target. Reduces estimation noise on small samples. Used in QuantOpsAI's portfolio risk model.

**Long-vol** — A position with positive vega (gains when volatility rises). Long calls, long puts, long straddles, long strangles are long-vol.

**Max favorable excursion (MFE)** — Highest unrealized profit a position reached during its lifetime, before being closed. The "MFE capture ratio" = realized P&L / MFE — measures how well exit logic captures available profit.

**Mean reversion** — Statistical tendency for prices to return toward an average. Strategy class: bet on extremes reverting.

**Meta-model** — A second-layer classifier that learns "given everything the primary model saw, was the primary model right?" Outputs a probability used to re-weight or suppress the primary model's predictions. QuantOpsAI's meta-model is two-layer: GBM batch + SGD freshness.

**Monte Carlo** — Simulation technique: repeatedly sample from input distributions, compute output, aggregate the empirical output distribution. Used in QuantOpsAI for portfolio VaR estimation and backtest variance estimation.

**OCC symbol** — Standardized 21-character option contract identifier (Options Clearing Corporation). Format: `<root padded to 6 chars><YYMMDD><C|P><strike × 1000 padded to 8 digits>`. E.g. `AAPL  250516C00150000` = AAPL May 16 2025 $150 call.

**OOS** — *Out-of-sample*. A holdout dataset not used in training. Strategy backtest gauntlet checks OOS Sharpe ≥ 70% of in-sample.

**Participation rate** — Order size as a fraction of [ADV](#adv). 1% participation = order is 1% of typical daily volume. Used in [Almgren-Chriss](#almgren-chriss) impact estimate.

**PDUFA** — *Prescription Drug User Fee Act* date. The FDA's deadline to approve/reject a drug application. A binary catalyst event for biotech stocks. Tracked via QuantOpsAI's `pdufa_events` table.

**Platt scaling** — Logistic-regression calibration layer (see [calibrator](#calibrator-platt-scaling)).

**Prediction type** — Categorical column on `ai_predictions`: `directional_long`, `directional_short`, `exit_long`, `exit_short`. Used to apply the right resolution rule and to scope per-direction tuning.

**Realized vol** — Statistical (historical) volatility of an asset, computed from past returns (typically annualized standard deviation of log returns). Compared to [implied vol](#implied-volatility) for vol-arbitrage signals.

**RegSHO** — SEC Regulation SHO. Governs short selling, including the FINRA daily short-volume report (free public data).

**Rho** — Option Greek: sensitivity to interest rates. Less important than delta/gamma/vega/theta for short-dated options.

**Sharpe ratio** — Annualized return / annualized volatility. Risk-adjusted return measure. Sharpe > 1 is good; > 2 is excellent.

**SGD** — *Stochastic Gradient Descent*. Iterative optimization: update model parameters using one sample (or a small batch) at a time. Used in QuantOpsAI's online meta-model freshness layer.

**Slippage** — Difference between expected price (decision price) and actual fill price. Adverse direction is positive (paying more on buys, receiving less on sells). Modeled by QuantOpsAI's 4-component slippage model.

**Survivorship bias** — Statistical error caused by analyzing only currently-existing entities (which excludes those that failed). Backtests run on today's universe inflate apparent performance because losers are removed. QuantOpsAI's `historical_universe_augment` tracks symbol delistings to correct for this.

**Squeeze (short squeeze)** — Rapid price increase in a heavily-shorted stock as shorts cover, forcing further buying. QuantOpsAI's squeeze-risk filter blocks shorts on names with high short-interest + low float.

**Stat-arb** — *Statistical arbitrage*. Trading strategy class based on statistical relationships between assets, typically [cointegration](#cointegration) or correlation. QuantOpsAI's pair book is one form.

**Term structure** — Relationship between option implied volatilities at different expiries. See [contango](#contango) / [backwardation](#backwardation).

**Theta** — Option Greek: sensitivity to time. Long options have negative theta (lose value as time passes). Short options have positive theta. Theta accelerates near expiry.

**Track record (per-stock, per-signal)** — QuantOpsAI's per-symbol prediction outcome history, split by signal type (BUY / SHORT / SELL / HOLD). Surfaced in the AI prompt to prevent confabulation.

**Trail percent** — A trailing stop's distance below the high-water mark, as a percentage. Stop level rises with the high-water mark; only triggers when price falls by `trail_percent` from the peak.

**Value-at-Risk (VaR)** — Statistical maximum expected loss over a given horizon at a given confidence. VaR_95 = loss not exceeded 95% of the time. Computed in QuantOpsAI both parametrically (from σ × Z-score) and via Monte Carlo.

**Vega** — Option Greek: sensitivity to volatility. Long calls and long puts both have positive vega.

**VWAP** — *Volume-Weighted Average Price*. Mean trade price weighted by volume. Common intraday execution benchmark. QuantOpsAI's `pct_from_vwap` is a feature.

**VIX** — CBOE Volatility Index. Implied vol of 30-day SPX options. The market's "fear gauge."

**Walk-forward** — Backtest methodology that respects time. Train on dates [t_0, t_1], test on [t_1, t_2], roll forward. Prevents look-ahead bias.

**Wash trade** — Buying and selling the same security within a short window (typically 30 days). SEC rules prohibit certain wash-trade patterns. QuantOpsAI catches Alpaca's wash-trade rejections and applies a 30-day cooldown.

**Wheel (options wheel)** — Cyclic options strategy: cash → cash-secured put → assigned → shares → covered call → called away → cash. Generates premium income on a watchlist of stable underlyings.

---

For terms in the codebase that aren't here, search the relevant doc:

- Code identifier (column, feature, signal): `docs/05_DATA_DICTIONARY.md`.
- Risk control: `docs/08_RISK_CONTROLS.md`.
- Strategy name: `docs/03_TRADING_STRATEGY.md`.
- AI/ML term: `docs/02_AI_SYSTEM.md`.
- Architectural term: `docs/04_TECHNICAL_REFERENCE.md`.
