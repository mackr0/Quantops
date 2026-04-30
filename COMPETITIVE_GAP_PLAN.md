# Competitive Gap Closure Plan

What separates this system from a real billion-dollar quant fund, and
which gaps we can credibly close ourselves vs. which we have to buy.

**Bias:** build before buy. Buy only when (a) the data is genuinely
proprietary, (b) the cost of building is materially higher than
buying, or (c) the build path requires capabilities we don't have
(e.g., exchange membership for market making).

## What we have today

The system is competitive with a small quant shop on these axes:

- 10 profiles × shared 3 paper accounts; per-profile virtual ledger.
- Multi-strategy (~25 strategies, 10 dedicated short).
- Specialist ensemble (4 AIs) + meta-model for candidate filtering.
- Auto-strategy generation + alpha-decay deprecation.
- Per-direction Kelly sizing, drawdown-aware capital scaling, risk-parity
  position sizing, market-neutrality enforcement.
- Broker-managed protective stops (trailing).
- Real factor exposure tracking (book/value, beta, momentum).
- 8-10 alternative data sources (insider, options, FINRA short volume,
  congressional, biotech, StockTwits, etc.).
- Self-tuning across 30+ parameters per profile.

Not competitive with a real fund because of everything below.

---

## Gap 1 — Strategy diversity (HIGHEST LEVERAGE)

### 1a. Options trading layer
**Real funds:** trade options for hedging, IV/vega plays, defined-risk
income (covered calls), defined-risk tail-protection (long puts), and
volatility arbitrage.

**Us:** read-only IV regime data on equity strategies. We don't trade
options.

**Build path:** Alpaca paper supports options trading via API. We
need:
- Option chain reader (Alpaca + yfinance fallback)
- Black-Scholes / Greeks computation (free `py_vollib` or implement)
- Strategy primitives: long put, long call, covered call, cash-secured
  put, vertical spread, iron condor, calendar spread
- IV rank computation (we have IV regime — this is more granular)
- Options-unusual-activity from volume + open interest changes
- Position sizing for options (defined-risk = different math)

**Buy alternative:** None really — Alpaca options API is free, basic
options data via Alpaca + yfinance is sufficient for paper, decent
options-flow services (Black Box Stocks, Cheddar Flow) are
$50-300/mo and partially redundant with what we can compute ourselves.

**Effort:** 1-2 weeks (focused).
**Edge gain:** large — opens hedging (long stock + protective put),
IV mean-reversion (sell rich vol on overhyped names), and defined-
risk income (covered calls on existing longs in low-vol regimes).
Probably 20-40% additional risk-adjusted return potential.

### 1b. Statistical arbitrage at scale
**Real funds:** trade hundreds to thousands of cointegrated pairs
simultaneously. Pair regime detection (when cointegration breaks).
Multi-leg basket trades.

**Us:** one pair-trade primitive (P2.3) that surfaces 1-3 candidate
pairs to the AI per cycle. The AI has to pick — there's no portfolio
of pairs.

**Build path:** all in `statsmodels` (free):
- Universe scanner: pairwise Engle-Granger cointegration test on the
  universe over 60-180 day windows
- Rank by p-value × half-life × correlation distance
- Maintain a pair book of 50-200 active pairs with Z-score state
- Entry at ±2σ Z, exit at 0σ or stop at ±3σ (regime break)
- Dollar-neutral or beta-neutral pair sizing
- Cross-pair correlation guard (don't load 10 correlated pairs)
- Auto-eject pairs whose cointegration p-value > 0.10 in the rolling
  test (regime break)

**Buy alternative:** None worth it — pair-trade infrastructure is
either DIY or ultra-expensive (Bloomberg PRMS, BarraPM).

**Effort:** 2 weeks.
**Edge gain:** large — stat-arb is one of the most scalable, market-
neutral edge sources. Real-money funds run this in size; the math is
public.

### 1c. Volatility strategies
**Real funds:** sell premium when IV rich, buy when IV cheap, term
structure arbitrage (trade contango/backwardation in VIX futures).

**Us:** read IV; don't trade vol.

**Build path:**
- Build atop the options layer (1a)
- IV rank + IV percentile per name
- Strategy: short strangles when IV rank > 80, long strangles when < 20
- Long-vol portfolio hedge (long VIX exposure during drawdowns)

**Buy alternative:** Cheddar Flow ($50-100/mo) for unusual-flow
signals. Useful but not strictly necessary.

**Effort:** 1 week (after 1a is in).
**Edge gain:** moderate-large in volatile regimes; modest in calm.

---

## Gap 2 — Risk modeling (MEDIUM-HIGH LEVERAGE)

### 2a. Barra-style multi-factor model
**Real funds:** Barra/Axioma 50-100 factor risk models with
covariance matrices, portfolio-level VaR, stress testing against
historical scenarios.

**Us:** 3 factors (book/market, beta, momentum). No covariance
matrix, no portfolio VaR.

**Build path:** Use Ken French's free factor library (Mom, Size,
Value, Investment, Profitability all free at
`mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html`).
- Returns regression per stock to estimate factor exposures
- Idiosyncratic vol = residual variance after factor explanation
- Covariance matrix = factor exposures × factor cov + idio diagonal
- Portfolio variance from factor exposures + idio
- Daily VaR / expected shortfall via Monte Carlo from this cov matrix
- Stress scenarios: 2008, 2020, dot-com, 1987 historical replays

**Buy alternative:** AxiomaPM / MSCI BarraOne / Bloomberg PRMS — all
$10K-100K+/yr. Way more than we need at our scale.

**Effort:** 2-3 weeks.
**Edge gain:** moderate (better risk awareness; not direct alpha).
But essential for credibility.

### 2b. Intraday risk monitoring
**Real funds:** real-time book P&L, exposure, factor drift; auto-
flatten / reduce on threshold breaches.

**Us:** 5-min cycle exposure recompute. No real-time alerts.

**Build path:** add a lightweight monitoring loop (10-30s tick) that
recomputes book exposure from cached positions + live quotes; alerts
on configured thresholds; auto-flatten path for "panic" thresholds.

**Buy alternative:** N/A.

**Effort:** 1 week.
**Edge gain:** low alpha but high tail-risk reduction. Worth it.

---

## Gap 3 — Data depth (MIXED LEVERAGE)

### 3a. Web-scraped alt data (BUILD)
Things we don't have but can scrape FREE:
- Reddit subreddit activity per ticker (r/wallstreetbets, r/stocks)
- StockTwits message volume + sentiment (we have basic, could go
  deeper with NLP)
- Google Trends search interest per ticker
- Wikipedia page-view spikes (proxy for retention/buzz)
- GitHub commit activity for tech companies
- Job-posting volume (Indeed/LinkedIn API or scrape) as company-
  growth proxy
- App store rankings (basic Apple/Google APIs free; deeper data paid)
- News headline sentiment from RSS feeds (we have basic — could
  expand to broader source set + better NLP)
- Earnings call transcript NLP (free via SEC filings; sentiment via
  open models)
- 10b5-1 insider planned-sale tracking (SEC EDGAR — we have basic
  insider, this is more granular)

**Effort:** 1 source per few days. Add 5-10 sources over 2-3 weeks.
**Edge gain:** small per source, real in aggregate. Best added
incrementally as the meta-model trains on each.

### 3b. Earnings-call sentiment via NLP (BUILD)
**Real funds:** use real-time earnings call transcript analysis with
custom-tuned NLP (or LLMs).

**Build path:** SEC 8-Ks include earnings press releases (free).
Earnings call audio is harder — could buy or transcribe via Whisper.
Sentiment analysis via the existing AI (we already use Claude for
specialist analysis — point it at recent earnings text).

**Effort:** 1 week.
**Edge gain:** moderate. Earnings drift is a known anomaly.

### 3c. Things we have to BUY (cheap)
- **Quiver Quant Premium ($30-100/mo):** government contracts,
  patents, lobbying, app downloads. Things we can't easily scrape.
- **Polygon.io basic ($50/mo):** better real-time data feed +
  options data than Alpaca free tier. Only matters when going to
  real money.
- **Maybe Benzinga Pro ($150/mo):** real-time news + unusual options.
  Defer until 1a + 1c are in.

**Total external data spend recommendation:** $30-50/mo (just Quiver)
until going to real money. Then add Polygon.

---

## Gap 4 — Multi-asset (MEDIUM LEVERAGE)

### 4a. Futures + FX via IBKR
**Real funds:** trade across asset classes with cross-asset hedges.

**Us:** equity-only via Alpaca.

**Build path:** add IBKR adapter alongside Alpaca adapter. IBKR has
free paper trading + futures + FX + options. Build a broker
abstraction so trade_pipeline can route to either.

**Buy alternative:** N/A — IBKR is free for paper.

**Effort:** 1 month. Significant infrastructure work because the
order/position model differs.
**Edge gain:** large — opens commodity / rates / FX strategies and
real cross-asset hedging. But infrastructure-heavy.

### 4b. Crypto (BUILD)
We technically support crypto via the segment system. Mostly
unused. Defer until we have a real strategy thesis.

---

## Gap 5 — Model sophistication (MEDIUM LEVERAGE)

### 5a. Online / continuous learning
**Real funds:** continuous model updates as outcomes resolve.

**Us:** daily retrain.

**Build path:** scikit-learn `partial_fit` or `river` library (free).
Re-weight feature importance on each resolved prediction. Per-trade
update of meta-model probabilities.

**Effort:** 1-2 weeks.
**Edge gain:** moderate, especially in regime breaks.

### 5b. Adversarial / red-team specialist
**Real funds:** independent risk teams critique trades pre-execution.

**Us:** none.

**Build path:** 5th specialist in the ensemble whose role is "find
the failure mode in this trade." Looks for: correlation risk, single-
name concentration, regime mismatch, recent earnings, factor
exposure violations. VETO authority like risk_assessor.

**Effort:** 1 week.
**Edge gain:** moderate (defensive — catches obvious bad trades).

### 5c. Better backtesting infrastructure
**Real funds:** walk-forward, regime-conditional, transaction-cost-
aware, Monte Carlo with realistic slippage.

**Us:** rigorous_backtest with 10 gates. Decent but limited.

**Build path:** add walk-forward windowing, regime-conditional
performance reporting, slippage modeling tied to ADV, Monte Carlo
with bootstrap from actual fill distributions.

**Effort:** 2 weeks.
**Edge gain:** moderate — better strategy selection / parameter
choice means better forward P&L.

---

## Gap 6 — Operational (LOW-MEDIUM LEVERAGE)

### 6a. Real money via IBKR Pro
**Why:** every metric is theoretical until real fills happen.

**Build path:** N/A — open account (free), small starting capital.
Wire IBKR adapter from 4a.

**Effort:** account opening (days) + adapter (within 4a).
**Edge gain:** N/A directly. But required for credibility.

### 6b. Capital allocation across strategies
**Real funds:** strategy-level Kelly + dynamic capital reallocation
based on rolling Sharpe.

**Us:** per-trade Kelly. Per-strategy capital allocation is basic.

**Build path:** track Sharpe per strategy_type over rolling 30/60/90
day windows; allocate capital inverse to volatility × inverse to
Sharpe. Already partially in `compute_capital_allocations`; expand.

**Effort:** 1 week.
**Edge gain:** moderate.

---

## Explicitly NOT pursuing

- **Latency arbitrage:** requires sub-microsecond execution + co-
  location. Not buildable in our cost envelope.
- **Market making:** requires exchange membership + low-latency
  infra. Capital + relationships, not software.
- **Block trading capacity:** our trades are too small to matter.
- **Index inclusion arbitrage:** requires capacity to move millions
  in seconds at the close.
- **Insider information networks:** legal versions (paid expert
  networks) are expensive and not particularly useful at our scale.

These are real differentiators of billion-dollar funds but the gap
is structural, not addressable in software.

---

## Recommended sequencing

By edge-per-week ratio, build first:

| Order | Item | Effort | Build/Buy | Cost | Edge |
|---|---|---|---|---|---|
| 1 | **1a Options trading layer** | 1-2 wk | BUILD | $0 | LARGE |
| 2 | **5b Adversarial reviewer specialist** | 1 wk | BUILD | $0 | MEDIUM |
| 3 | **1b Stat-arb pair book** | 2 wk | BUILD | $0 | LARGE |
| 4 | **6b Strategy-level capital allocation** | 1 wk | BUILD | $0 | MEDIUM |
| 5 | **2b Intraday risk monitoring** | 1 wk | BUILD | $0 | TAIL |
| 6 | **1c Volatility strategies (after 1a)** | 1 wk | BUILD | $0 | MEDIUM |
| 7 | **2a Barra-style factor model** | 2-3 wk | BUILD | $0 | MEDIUM |
| 8 | **3a Web-scraped alt data (incremental)** | 2-3 wk | BUILD | $0 | SMALL-MED |
| 9 | **3c Quiver Premium subscription** | 1 day | BUY | $50/mo | SMALL-MED |
| 10 | **5a Online learning** | 1-2 wk | BUILD | $0 | MEDIUM |
| 11 | **3b Earnings call NLP** | 1 wk | BUILD | $0 | MEDIUM |
| 12 | **5c Better backtesting** | 2 wk | BUILD | $0 | FOUNDATION |
| 13 | **4a IBKR multi-broker / multi-asset** | 1 mo | BUILD | $0 | LARGE |
| 14 | **6a Graduate to real money** | days | BUILD+BUY | starting capital | REQUIRED |

**Total build effort:** ~3-4 months focused work.
**Total external spend:** $50/mo (Quiver) until real-money phase,
then ~$200/mo (add Polygon) — still under $3K/yr total.

After all of this, the system would be competitive with a small
quant shop ($50-200M AUM equity-derivatives focus). Still not
Citadel — Citadel's edge at scale is capital, headcount, multi-asset
infrastructure, and physical co-location, not software.

## Decision points before each item

Before building each, validate:
1. Is the build cost still <2× the buy cost at our scale?
2. Has the edge thesis been confirmed by paper-trading the simpler
   version first?
3. Are we still under the operational complexity budget (i.e., does
   adding this make existing things worse to maintain)?

Re-rank if any of these flip during build.
