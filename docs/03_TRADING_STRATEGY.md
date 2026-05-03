# 03 — Trading Strategy

**Audience:** finance professionals, strategy researchers, anyone evaluating what the system actually trades.
**Prerequisites:** working knowledge of equity / options market structure, position sizing math, factor models.
**Last updated:** 2026-05-03.

This document describes WHAT the system trades, HOW it sizes, and HOW it manages risk in finance terms — without dwelling on software architecture (`docs/04`) or the AI/ML internals (`docs/02`).

## 1. Universe and segmentation

The platform trades US equities and options on those equities. Universe construction is dynamic, not hardcoded — every cycle pulls Alpaca's active asset list, which carries ~8,000 tradeable US-listed names.

Each profile is constrained to a **market segment**:

| Segment | Approximate price range | Liquidity floor | Strategies emphasized |
|---|---|---|---|
| `largecap` | $50-$500+ | $50M+ daily $vol | Momentum continuation, sector rotation, mean reversion at scale, options income (covered calls, cash-secured puts) |
| `midcap` | $20-$100 | $10M+ daily $vol | Catalyst-driven moves, earnings drift, breakout patterns, balanced long/short |
| `smallcap` | $5-$20 | $1M+ daily $vol | Volatility breakouts, gap plays, short squeezes (long side), insider clusters |
| `microsmall` | $1-$5 | $500K+ daily $vol | Pure technical, news-driven, more conservative sizing |
| `crypto` | n/a | n/a | Crypto-specific strategies via Alpaca's crypto endpoint |
| `largecap_shorts`, `smallcap_shorts`, `mid_shorts` | (matching) | (matching) | Short-bias variants of above with bearish strategy library |

Segment definition lives in `segments.py` (live) and `segments_historical.py` (frozen baseline used for backtest survivorship-bias correction).

## 2. Strategy library

The platform ships with 20+ deterministic strategies, organized by direction and methodology.

### 2a. Bullish strategies (16)

The first four are the legacy "core" strategies in `fallback_strategy.py` / `strategy_small.py` — controlled by the per-profile `strategy_momentum_breakout` / `strategy_volume_spike` / `strategy_mean_reversion` / `strategy_gap_and_go` toggle columns. The rest live as plugin modules in `strategies/`.

| Strategy | Edge claim | Confirmation signals |
|---|---|---|
| `momentum_breakout` | Stocks breaking above prior resistance with volume continuation | 20d high break + 1.5× avg volume + ADX > 25 |
| `volume_spike` | Unusual volume often precedes price moves | Volume ratio ≥ 2× 20d avg + price change >= 0 |
| `mean_reversion` | Oversold bounces in established uptrends | RSI ≤ 30 + price > 50d MA + bullish divergence |
| `gap_and_go` | Morning gaps that hold and extend | Gap ≥ 3% + first 30-min hold + volume confirmation |
| `gap_reversal` | Failed gaps reverse — trade the reversion | Gap up ≥ 3% but selling into open + reclaim of pre-market |
| `news_sentiment_spike` | Positive news with social-media confirmation | News score ≥ 0.7 + Reddit/StockTwits trending bullish |
| `short_squeeze_setup` (long side) | High short interest names breaking out | Short_pct_float ≥ 20% + breakout + reduced borrow availability |
| `earnings_drift` | Post-earnings drift on positive surprises | Earnings beat ≥ 5% + price gap + analyst revision streak |
| `insider_cluster` | Cluster of insider buys signals informed conviction | ≥ 3 insider buys in 30d + dollar volume threshold |
| `fifty_two_week_breakout` | 52-week high breaks tend to continue | 52w high break + volume + RSI < 80 (avoiding euphoria) |
| `macd_cross_confirmation` | MACD bullish cross with volume | MACD line crosses above signal + histogram expanding |
| `sector_momentum_rotation` | Names in top-3 sectors via 5d ETF returns | Symbol sector ∈ top-3 sectors by ETF rotation + symbol RS |
| `analyst_upgrade_drift` | Analyst rating upgrades produce sustained drift | Recent upgrade + price hasn't fully closed gap |
| `short_term_reversal` | 1-3 day reversal of overdone selling | Down ≥ 5% in 3 days + RSI ≤ 25 + at support |
| `volume_dryup_breakout` | Volume contraction precedes breakouts | Volume ratio ≤ 0.5× × N consecutive days, then expansion |
| `max_pain_pinning` | Index ETFs pin to max-pain near monthly opex | Days to monthly opex ≤ 3 + spot near max pain |

### 2b. Bearish strategies (10)

| Strategy | Edge claim |
|---|---|
| `breakdown_support` | Breaks below well-tested support with volume |
| `distribution_at_highs` | Topping pattern: price flat at highs while volume rises on red days |
| `failed_breakout` | Breakout attempts that fail and reverse |
| `parabolic_exhaustion` | Multi-day parabolic moves followed by reversal candles |
| `relative_weakness_in_strong_sector` | Names underperforming a strong sector's leaders |
| `relative_weakness_universe` | Universe-wide bottom-percentile 20d return ranker. Always-on (regardless of regime) — fills short books in extended bull markets where textbook bearish technical patterns are rare |
| `earnings_disaster_short` | Post-earnings drift inverse: misses + guidance cuts produce sustained downside drift |
| `catalyst_filing_short` | High-severity SEC filings (going-concern, material weakness, accounting restatements) |
| `sector_rotation_short` | Names in bottom-3 sectors by 5d ETF rotation |
| `iv_regime_short` | High realized vol + IV expansion = continuation short signal |
| `insider_selling_cluster` | Cluster of insider sells in a 30d window |
| `high_iv_rank_fade` | Long-vol unwinds (short volatility) when IV rank > 90 |
| `vol_regime` | Multi-symbol vol-regime detector that suppresses dip-buys in volatile regimes |

### 2c. Strategy selection flow

1. **Per-cycle**, every strategy runs on every symbol in the segment universe. Cheap — pure functions, no API cost.
2. Each strategy emits a vote: STRONG_BUY / BUY / HOLD / SELL / STRONG_SELL / SHORT / STRONG_SHORT.
3. Votes are aggregated per symbol; composite score combines vote counts, vote strengths, and per-strategy historical win rates.
4. **Top-30 candidates** by composite score advance, with reserved slots: top-10 longs + top-5 shorts when shorts are enabled. This protects short opportunities from being crowded out by a long-vote-dominated universe.
5. Each candidate is enriched with technicals, alt-data, options state, factor exposures, etc., before reaching the AI.

The AI's job is to **pick zero to three** trades from the enriched candidate list and size them. The AI never trades a candidate that didn't surface from the strategy votes — strategies are the funnel, the AI is the discriminator and sizer.

### 2d. Strategy lifecycle

- **Probationary period.** New strategies (auto-generated by `strategy_proposer.py`, or hand-added) run through the rigorous backtest gauntlet (`docs/02_AI_SYSTEM.md` §10a) before being added to the live engine pool.
- **Alpha decay monitoring.** `alpha_decay.py` tracks per-strategy rolling 30d Sharpe vs lifetime baseline. Strategies that degrade for 30+ consecutive days are auto-deprecated; they no longer fire on the live engine but their historical contribution stays in the record. A 14+ day recovery period auto-restores them.
- **Auto-generation.** `strategy_proposer.propose_strategies` periodically commissions new variants by recombining successful signal patterns. Variants share the strategy_type column for tracking.

## 3. Options program

The options layer ships **5 single-leg primitives** (in `options_trader.py`) and **11 multi-leg primitives** (in `options_multileg.py`). Single-leg builders are: `build_long_call`, `build_long_put`, `build_covered_call`, `build_cash_secured_put` (4 dedicated builders); `protective_put` is a recognized strategy type executed via `execute_option_strategy` without a dedicated builder, since it's structurally a long stock leg + a long put leg.

| Primitive | Direction / structure | When the system uses it |
|---|---|---|
| `long_call`, `long_put` | Single-leg directional | Bullish/bearish thesis with cheap IV; defined risk |
| `covered_call` | Income on long stock | Existing long position + low IV rank + sideways thesis |
| `cash_secured_put` | Get-paid-to-buy | Want to enter a long position; collect premium for downside risk |
| `protective_put` | Long stock + put hedge | High-conviction long with elevated tail risk |
| `bull_call_spread`, `bear_put_spread` | Vertical debit (multi-leg) | Directional thesis with capped premium |
| `bull_put_spread`, `bear_call_spread` | Vertical credit (multi-leg) | Directional thesis with premium-collection bias |
| `iron_condor` | Range-bound short premium (multi-leg) | High IV rank + low realized vol expectation |
| `iron_butterfly` | Pinned-to-strike short premium (multi-leg) | Tight expected range + max-pain pinning thesis |
| `long_straddle`, `short_straddle`, `long_strangle` | Pure vol play (multi-leg) | Pre-event when IV cheap (long) or rich (short) |
| `calendar_spread`, `diagonal_spread` | Term-structure plays (multi-leg) | Backwardation (front rich) or contango (back rich) opportunities |

The options program is governed by:

- **Greeks aggregator** (`options_greeks_aggregator.py`): rolls per-position Greeks into book-level net delta, gamma, vega, theta. Three exposure gates on UserContext (`max_net_options_delta_pct`, `max_theta_burn_dollars_per_day`, `max_short_vega_dollars`) block entries that would breach the book's overall risk profile.
- **Vol regime classifier** (`options_vol_regime.py`): translates raw IV signals (rank, skew, term) into strategy guidance — premium_rich → iron condors / credit spreads; premium_cheap → debit spreads / long straddles; term_backwardation → long-front diagonals.
- **Roll manager** (`options_roll_manager.py`): auto-closes credit positions at ≥80% max profit; recommends rolls above 50%. Window and thresholds are now per-profile knobs (OPEN_ITEMS #10).
- **Lifecycle handler** (`options_lifecycle.py`): detects assignment / exercise on expiry and writes synthetic equity legs.
- **Wheel state machine** (`options_wheel.py`): cash → CSP → assigned → shares → CC → called away → cash. Per-symbol opt-in via `wheel_symbols` (UserContext list field).
- **Delta hedger** (`options_delta_hedger.py`): for long-vol option positions only, submits stock-side rebalance when |delta drift| > max(5 shares, 5%).

### Options strategy advisor

`options_strategy_advisor.py` evaluates each held position for opportunistic options strategies (covered call on a stable long, protective put on a high-conviction long with elevated IV). It emits recommendations into the AI prompt — the AI decides whether to act.

### Macro event opportunism (Phase F2)

`macro_event_tracker.py` carries a hand-curated FOMC / CPI / NFP calendar through end of 2026. `evaluate_macro_play` mirrors the earnings-vol template: pre-window IV rich → SPY iron condor; pre-window IV cheap → long straddle; post-window → time-stop existing macro plays. Calendar refresh is manual; events are ~stable (Fed publishes annually).

## 4. Statistical arbitrage

`stat_arb_pair_book.py` runs an Engle-Granger cointegration scanner over the universe weekly, retains pairs with p-value < 0.05, half-life in 5-30 days, and correlation > 0.7. The book persists to `stat_arb_pairs` per profile. Daily retest re-runs Engle-Granger and ejects pairs whose p-value drifts above 0.10 (regime break).

Trade signals on actionable pairs:

- **Z-score ≥ +2σ:** enter SHORT A / LONG B (spread will revert toward 0).
- **Z-score ≤ −2σ:** enter LONG A / SHORT B.
- **Z-score crosses 0:** exit.
- **Z-score ≥ ±3σ:** stop loss (regime likely broken).

Sizing is dollar-neutral: equal dollars per leg. The AI can act on the surfaced pair via `action: PAIR_TRADE` with `symbol_a`, `symbol_b`, `pair_action`, `dollars_per_leg`. Pair trades require both legs (one long + one short), so the feature is opt-in via `enable_stat_arb_pairs` (off by default; requires shorts enabled).

## 5. Position sizing

The system layers four independent sizing modifiers, each clamped, each defaulting to 1.0× when unknown:

```
final_size = base × kelly_modifier × drawdown_scale × vol_scale × strategy_weight
```

### 5a. Base size

Per-profile `max_position_pct` (default 10% of equity), with `short_max_position_pct` defaulting to half (5%) for short side — asymmetric-risk convention since short positions have unbounded upside risk.

### 5b. Kelly modifier (P4.2 of long/short build)

Per-direction fractional Kelly. `kelly_sizing.compute_kelly_recommendation(db_path, direction)`:

```
f* = (b·p − q) / b × fractional
```

where p = win rate, q = 1-p, b = avg_win / avg_loss, fractional = 0.25 (quarter Kelly is the default — full Kelly is too aggressive in practice given uncertainty in parameter estimates).

Reads only entry signals (BUY/STRONG_BUY for longs; SHORT/STRONG_SHORT for shorts). HOLD-as-loss exits get filtered out — they reflect existing-position drift, not new bets.

Surfaced to the AI prompt as `LONG: Kelly 11.8% (WR 70%, avg win 2.95%, avg loss 2.23%, n=30)`. Empty when neither direction has ≥30 resolved entry trades with positive edge.

### 5c. Drawdown capital scale (P4.3)

`drawdown_scaling.compute_capital_scale(drawdown_pct)` is a continuous size modifier on [0.25, 1.0]. Linear interpolation between breakpoints:

| Drawdown | Scale |
|---|---|
| 0% (or above peak) | 1.00 |
| 5% | 0.85 |
| 10% | 0.65 |
| 15% | 0.45 |
| 20%+ | 0.25 |

Independent of the discrete crisis-state action (which can also hard-pause). The scaling shrinks the entries that DO happen.

### 5d. Risk-budget / vol scaling (P4.4)

`risk_parity.analyze_position_risk(positions, equity)` computes per-position `weight × annualized_vol` contributions. Names ≥ 2× or ≤ 0.5× the per-position average are flagged. Sizing rule: `vol_scale = target_vol / realized_vol_i`, clamped to [0.4, 1.6]. So a 60%-vol biotech gets 0.4× the base size; a 15%-vol utility gets up to 1.6×.

Vols cached 7d via `factor_data.get_realized_vol`.

### 5e. Strategy weight (`strategy_capital_allocator.py`)

Per-strategy weight in the trade pipeline, computed as `score = sharpe × (1 + win_rate)`, normalized to mean 1.0 across strategies, clamped [0.25, 2.0]. Median imputation for new strategies (n < 10 samples). Applied to `size_pct` for BUY / SHORT / SELL actions.

### 5f. Hard caps (validation gates)

- **Asymmetric short cap:** longs sized against `max_position_pct`; shorts capped at `short_max_position_pct`.
- **HTB borrow penalty:** hard-to-borrow shorts have their cap halved again.
- **Net-exposure rebalance gate:** when book has drifted >25pp off `target_short_pct`, block new entries on the over-weighted side.
- **Market-neutrality gate:** when `target_book_beta` is set, block entries that push `|projected − target| − |current − target| > 0.5`. Symmetric — entries that improve neutrality always pass.
- **Max sector positions:** `max_sector_positions` (default 5) caps concentration per sector.
- **Correlation cap:** `max_correlation` (default 0.7) blocks new entries whose 30d return correlation with an existing position exceeds the threshold.

## 6. Exits

Every entry receives broker-managed protective orders (Phase 12 of the legacy roadmap; INTRADAY_STOPS_PLAN in archive):

- **Static stop loss** at entry × (1 − `stop_loss_pct`). Or
- **Trailing stop** with `trail_percent_for_entry` (clamped [2%, 10%]) when `use_trailing_stops=1` (default).

Exactly one protective order per position; both stop+TP+trailing on the same shares triggers an Alpaca qty-conflict so only one is used. Take-profit detection runs in the polling fallback (cycle-based) since TP isn't time-critical the way stops are.

When `use_conviction_tp_override=1` and a position hits its fixed take-profit, the system can SKIP the fixed TP and let the trailing stop manage the exit — provided the AI still has high conviction (`conviction_tp_min_confidence`), the trend is intact (`conviction_tp_min_adx`), and price is making new highs. Designed for runaway winners where fixed TP caps upside.

### Time stops on shorts

`short_max_hold_days` (default 10) covers any short older than the threshold regardless of P&L — multi-day shorts accumulate borrow cost and crowding risk.

## 7. Risk management — the layered defenses

Six independent risk controls. Any one of them is sufficient. They are NOT cumulative — they are independent guards.

### 7a. Crisis state monitor (`crisis_detector.py` + `crisis_state.py`)

Cross-asset distress signals trigger an elevated → crisis → severe state machine:

- VIX absolute level + term structure inversion
- SPY / TLT / GLD / UUP correlation spikes
- Bond/stock divergence
- Gold safe-haven rallies
- HYG/LQD credit spreads widening
- Cluster of recent price shocks across held positions

State levels affect position sizing:

| Level | Size multiplier | New long entries |
|---|---|---|
| `normal` | 1.00× | Allowed |
| `elevated` | scaled per signal severity | Allowed |
| `crisis` | depends | Blocked |
| `severe` | 0.0 (block all) | Blocked, plus consider liquidate |

Surfaced to AI prompt as `*** CRISIS STATE: ELEVATED (size x0.65) ***`.

### 7b. Intraday risk monitor (`intraday_risk_monitor.py`)

Four checks each cycle:

| Check | Threshold | Action |
|---|---|---|
| Drawdown acceleration | Today's drawdown ≥ 2× 7d avg | Block new entries (warning); pause all (critical at ≥3×) |
| Vol spike | SPY hourly vol ≥ 3× 20d avg | Block new entries |
| Sector swing | Largest sector move ≥ 3% | Block new entries |
| Held-position halts | ≥1 held name halted | Block new entries (warning); pause all (critical at ≥3) |

When any alert fires, a `risk_halt` row writes to the per-profile DB. New entries are blocked until 60-minute auto-clear.

### 7c. Per-trade stops

Broker-managed protective orders (above) at the broker. Polling-based fallback for take-profit detection.

### 7d. Portfolio risk model + stress scenarios

Already covered in `docs/02_AI_SYSTEM.md` §11. The relevant trader-facing point: every trade decision sees the daily portfolio σ, 95% VaR, 95% ES, top factor exposures, and worst-3 stress scenario projections in the prompt — so position sizing reasoning can be informed by tail risk, not just per-trade math.

### 7e. Long-vol portfolio hedge

`long_vol_hedge.py` opens SPY puts (5% OTM, ~45 DTE) when:

- Drawdown ≥ `long_vol_hedge_drawdown_pct` (default 5%) from 30-day equity peak, OR
- Crisis state ≥ "elevated", OR
- 95% VaR ≥ `long_vol_hedge_var_pct` (default 3%) of book.

Premium budget: `long_vol_hedge_premium_pct` (default 1% of book per active hedge). Auto-rolls when DTE < 14 or delta decayed past −0.10. Auto-closes when ALL triggers clear simultaneously.

Off by default. Opt-in (`enable_long_vol_hedge`) — costs real put premium.

### 7f. Balance / neutrality gates

Already covered. Blocks entries that materially push the book away from user-set long/short balance or beta targets.

## 8. Track record and learning

The system maintains a per-stock track record split by signal type (BUY / SHORT / SELL / HOLD), surfaced in the AI prompt as:

```
13W/0L overall (100%) — BUY 0W/0L (0%); SHORT 0W/0L (0%); HOLD 13W/0L (100%)
```

Splitting by signal type prevents confabulation: a 100% win rate on HOLDs is meaningless when the AI is now considering a SHORT on the same name. Without this split, the AI was empirically observed claiming "100% personal win rate on VALE shorts" when zero shorts had ever been resolved on VALE — all 13 wins were HOLDs.

Last-prediction reasoning is also surfaced (`Last call: BUY (75% conf, outcome: win). Reasoning: ...`) so the AI can see what it said last time and what happened.

Learned patterns from `post_mortem.py` are injected under `LEARNED PATTERNS` when active. These are clusters from losing-week analysis (e.g. "60% of losses had insider_cluster=high AND vwap_position=below").

## 9. Honest limits

- **Two weeks of data.** Per-strategy win rates, Kelly recommendations, and meta-model AUC are noisy estimates. The system is engineered to compound this asset over time but interpretation today must acknowledge the small sample.
- **Slippage paper-fitted.** Real-money fills will deviate. K should be re-calibrated after 30+ days of live trading.
- **Synthetic options backtester misses bid-ask, IV term, and catalyst vol.** Direction and magnitude are captured; precision P&L is not.
- **Stress scenarios miss rates / FX / commodities.** 2022-style rate shocks under-report.
- **Long-vol hedge bleeds premium in calm markets.** Default OFF for that reason.

## See also

- `docs/02_AI_SYSTEM.md` for the AI/ML technical detail.
- `docs/05_DATA_DICTIONARY.md` for every signal and feature key.
- `docs/08_RISK_CONTROLS.md` for the full kill-switch and gate enumeration.
