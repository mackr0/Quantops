# 06 — User Guide

**Audience:** the operator using QuantOpsAI to run paper-trading strategies. Assumes basic familiarity with stock and options trading.
**Purpose:** explain every screen, every setting, every dashboard signal, and the typical workflows.
**Last updated:** 2026-05-03.

## 1. First-time setup

The platform comes pre-configured for the operator who built it (single-user, local instance with prod deployed at `67.205.155.63`). For a fresh install:

1. Sign up for an Alpaca paper trading account (free) and generate API keys.
2. Sign up for at least one AI provider (Anthropic recommended for cost; OpenAI / Google work too).
3. Log in at `https://<your-domain>/`.
4. Settings → Alpaca Accounts → Add account → paste API keys. Repeat up to 3 times for multiple paper accounts.
5. Settings → Create new profile → choose name, market type, Alpaca account.

Each profile starts disabled. Enable the master toggle when ready to start scanning.

## 2. The Dashboard

Top of the page: the multi-profile equity overview.

For each profile:
- **Equity** (current account value)
- **Daily P&L** (today's change)
- **Total P&L** (since profile creation)
- **Open positions** count
- **Pending orders** (stops + limits at the broker)

Each profile-row links to the per-profile detail dashboard.

## 3. The AI Intelligence dashboard (`/ai`)

Four tabs:

### 3a. Brain tab

What the AI's "brain state" looks like.

| Panel | What it shows |
|---|---|
| **Performance summary** | Per-profile win rate, profit factor, AI prediction count, win rate by direction (long/short/exits). |
| **Win-Rate History Chart** | 30-day rolling win rate per profile. Drift up = improving, drift down = check what changed. |
| **Meta-Model** | Per-profile: GBM AUC, accuracy, training samples, top predictive features. SGD freshness layer: n_updates, n_features, last update timestamp. |
| **Slippage Model** | Calibrated K, sample count, mean residual, bucket sample counts, sample estimate component breakdown (half-spread + impact + vol + bootstrap = total bps). |
| **Slippage Calibration Drift** | Predicted vs realized for last 200 fills. Mean delta near zero = well-calibrated. Persistent positive delta = K under-predicting (your fills are worse than expected; bump K). Persistent negative = over-predicting. |
| **Synthetic Options Backtester** | Pick a symbol + strategy + lookback + OTM% + DTE + cycle days, click Run. Returns equity curve + win rate + profit factor over the historical window. |
| **Monte Carlo Backtest** | Run 1,000 MC sims on the profile's recent closed trades. Returns 5/25/50/75/95th percentile P&L distribution + P(loss). Wide [5%, 95%] band = strategy P&L is execution-variance-sensitive. |
| **Per-Strategy Monte Carlo** | Same MC engine, grouped by strategy. Shows which strategies have ROBUST edge vs apparent edge that vanishes under realistic slippage. |
| **AI Cost** | Today's spend, headroom vs ceiling, recent calls. |
| **Pair Book** | (Long/short profiles only.) Active cointegrated pairs with current Z-score + actionability flags. |
| **Greeks** | (Options profiles only.) Net delta / gamma / vega / theta across the book. |
| **Strategy Decay Monitor** | Per-strategy rolling 30d Sharpe vs lifetime baseline. Strategies in deprecation candidacy are flagged. |
| **Validation Results** | Recent backtest gauntlet runs. |
| **Capital Allocation** | Per-strategy weight from the strategy capital allocator. |

### 3b. Strategy tab

Per-strategy detail. For each strategy:
- Recent win rate, average P&L, sample count
- Restore button (if alpha-decay-deprecated)
- Audit trail of capital weight changes

### 3c. Awareness tab

What the AI sees right now, on this cycle.

| Panel | What it shows |
|---|---|
| **Long/Short Construction** | Per-profile: current vs target short share, current vs target book beta, Kelly recommendations per direction, drawdown capital scale. |
| **Risk Budget** | Per-position vol contributions; flags names ≥ 2× or ≤ 0.5× the per-position average. |
| **Sector Concentration** | Sectors ≥ 30% gross are flagged with the AI prompt warning. |
| **Portfolio Risk** (Item 2a) | Daily portfolio σ, parametric and Monte Carlo 95% / 99% VaR, Expected Shortfall, top factor exposures, risk decomposition (sectors / styles / French / idio), worst-3 stress scenarios. |
| **Market Intelligence** | Yield curve (2Y, 10Y, spread, inversion flag), CBOE Skew, ETF flows (sector net flows), FRED economic indicators. |
| **SEC Filing Alerts** | Recent 10-K / 10-Q / 8-K alerts on held positions and watchlist symbols. |
| **Crisis Monitor** | Cross-asset crisis state (normal / elevated / crisis / severe). When elevated: signals firing + size multiplier + readings + transition history. |
| **Event Stream** | Recent events (SEC, earnings, price shocks, halts) handled by the event bus. |
| **Specialist Ensemble** | Each candidate's per-specialist verdicts (earnings, pattern, sentiment, risk, adversarial). Vetoes highlighted. |
| **Attention Signals** | Per held position: Google Trends z-score + direction, Wikipedia 7d/90d z-score + spike flag, App Store ranks. |

### 3d. Operations tab

How the system is tuning itself.

| Panel | What it shows |
|---|---|
| **Self-Tuning** | Status pill per profile (resolved samples, can_tune, message), recent tuning history, parameter changes + reason. |
| **Cost Guard** | Daily spend tracking + headroom + ceiling. |
| **Active Lessons** | Post-mortem patterns + tuner-detected failure patterns being injected into the AI prompt. |
| **Tunable Signal Weights (Layer 2)** | Every weightable signal + current weight + override status. |
| **Autonomy Timeline** | Per-profile timeline of autonomous changes the system has made. |

## 4. Settings page (per profile)

Settings are organized into sections. Every numeric / boolean knob has a tooltip explaining what it does and what changing it affects.

### Identity & API keys

- **Profile name** — display label.
- **Market type** — dropdown.
- **Alpaca account** — which configured paper account this profile uses.
- **AI provider + model** — Anthropic / OpenAI / Google + model ID.
- **API keys** — set once; encrypted at rest.

### AI behavior

- **AI confidence threshold** — minimum AI confidence (0-100) to act on a trade. Default 25.
- **Self-tuning** — master toggle for the 12-layer self-tuner.
- **AI Model Auto-Tune (cost-sensitive)** — allows the tuner to A/B-test alternative models. Default OFF (avoids surprise Sonnet/Opus calls).

### Advanced Risk & Research Features

- **Intraday Risk Auto-Halt** (default ON) — drawdown / vol / sector / halt monitor; auto-blocks new entries during alerts.
- **Portfolio Risk Daily Snapshot** (default ON) — Barra-style risk model + stress scenarios; surfaced in AI prompt.
- **Statistical Arbitrage Pair Book** (default OFF) — requires shorts enabled; weekly cointegration scan + daily retest + Z-score signals.
- **Long-Vol Portfolio Hedge** (default OFF) — opens SPY puts on drawdown / crisis / VaR triggers; costs real put premium.
  - Drawdown trigger: default 5%
  - VaR trigger: default 3%
  - Premium budget: default 1% of book per hedge

### Screener

- Min/max price, min volume, volume surge multiplier, RSI overbought/oversold, momentum gain thresholds, gap threshold.

### Strategy toggles

- Enable / disable: momentum_breakout, volume_spike, mean_reversion, gap_and_go.
- (Other strategies are always on; toggle individual ones via Layer 2 weight = 0.)

### Risk

- Stop loss, take profit, max position, max total positions, max correlation, max sector positions.
- Drawdown pause / reduce thresholds.
- ATR-based stops (toggle + multipliers).
- Trailing stops (toggle + multiplier).
- Limit orders (toggle).

### Long/short

- **Enable Short Selling** — master toggle.
- Short stop loss, take profit, max position, max hold days.
- **Target short %** — desired short share of gross book (0 = long-only).
- **Target book beta** — desired gross-weighted book beta (blank = no target).

### Conviction TP override

- Skip fixed take-profit when AI conviction is high + ADX confirms trend + price making new highs.

### Multi-model consensus

- Run a secondary AI for cross-validation on each prompt.

### Custom watchlist

- Comma-separated additional symbols always traded.

### Wheel symbols

- Comma-separated symbols opted into the options wheel cycle (cash → CSP → assigned → shares → CC → called away → cash). Recommend stable, dividend-paying names where assignment is OK.

### Options roll-window thresholds

- **Roll window (days):** how many days before expiry the auto-close manager evaluates each open credit position. Default 7.
- **Auto-close at % of max profit:** auto-close credit positions at this fraction. Default 0.80. Tighter (0.60) reduces late-cycle assignment risk; looser (0.90) squeezes more premium.
- **Roll-recommend at % of max profit:** surface roll candidates above this. Default 0.50.

### Schedule

- Market hours (default), extended hours, or custom session window.

### Earnings / time-of-day

- Avoid earnings days (default 2): skip stocks with earnings within N days.
- Skip first minutes (default 0): skip first N minutes of session.

## 5. Trades page (`/trades`)

Trade ledger across all profiles.

- Filters: profile, symbol, side, status, date range.
- Each row: timestamp, symbol, side, qty, price, fill price, slippage_pct, P&L, AI confidence, AI reasoning (truncated, expandable).
- Click a row → trade detail with full reasoning + features at decision + outcome.

## 6. Performance page (`/performance`)

Per-profile performance breakdown.

- Equity curve.
- Daily / weekly / monthly P&L.
- By Sector / By Direction tables (gross + net %, position counts).
- Risk-budget breakdown.
- MFE capture ratio.
- Slippage stats (avg, worst, total adverse $).

## 7. Common workflows

### 7a. "I want to test a new strategy idea"

1. Settings → create new profile (or use existing).
2. Layer 2 weight tuning: AI Operations tab → Tunable Signal Weights → set weights to favor your new approach.
3. Or modify a strategy in `strategies/<name>.py` (engineering work).
4. Watch the Strategy tab for win rate over the next 30+ trades.
5. Use Synthetic Options Backtester or Monte Carlo Backtest to validate over a longer historical window.

### 7b. "I want to tighten risk on a profile"

- Lower `max_position_pct`.
- Tighten `max_correlation` (e.g. 0.7 → 0.5).
- Tighten `drawdown_reduce_pct` (e.g. 0.10 → 0.05).
- Enable Long-Vol Portfolio Hedge.
- Enable Intraday Risk Auto-Halt (default already ON).
- Set `target_book_beta = 0` for market-neutral.

### 7c. "I want to focus a profile on shorts"

- Enable Short Selling.
- Set `target_short_pct = 0.5` (50% of book) or higher.
- Set `target_book_beta = 0` for market-neutral.
- Optionally enable Statistical Arbitrage Pair Book.

### 7d. "The AI is making bad calls — what do I check?"

- AI Brain tab → Meta-Model: is AUC > 0.55? If not, the meta-model isn't yet trained or has poor signal.
- Awareness tab → Specialist Ensemble: which specialists are voting? Any consistently anti-correlated?
- Operations tab → Active Lessons: are there stale post-mortem patterns being injected that no longer apply?
- Operations tab → Self-Tuning: did the tuner recently change a parameter in a way that hurt performance?
- Trades page: filter to losses; read the AI reasoning. Pattern that emerges?

### 7e. "Cost is too high"

- Operations tab → Cost Guard: see the spend distribution.
- Lower the daily cost ceiling (in the user-settings page).
- Switch profiles to Haiku / GPT-mini variants where Sonnet isn't needed.
- Disable the most expensive specialists that aren't well-calibrated.

### 7f. "I want to wipe a profile and start fresh"

1. Disable the profile.
2. Cancel any open orders at the broker.
3. (Manual DB step) `rm /opt/quantopsai/quantopsai_profile_<id>.db`.
4. Re-enable the profile. The next cycle will create a fresh DB.

**Caveat:** see `docs/02_AI_SYSTEM.md` and the relevant CHANGELOG entry — wiping deletes the proprietary asset (resolved AI predictions). Almost always preferable to keep the data.

## 8. The virtual account model in practice

10+ profiles share 3 paper accounts. Things to know:

- A trade on profile 5 affects the broker account it's mapped to (e.g. Account 2). Profile 8's broker view INCLUDES profile 5's positions if both are mapped to Account 2.
- Per-profile dashboards and per-profile P&L are computed from each profile's `trades` ledger via FIFO accounting — these are the authoritative per-profile numbers.
- The cross-account reconciler runs daily and warns if drift is detected (sum of virtual ≠ broker actual).
- Pending orders panels are filtered to per-profile owned IDs (you don't see other profiles' orders even when broker accounts are shared).

## 9. Troubleshooting checklist

- **Scheduler not running:** check `/api/scheduler-status` for last cycle time per profile. If stale, scheduler may have crashed; restart it (`systemctl restart quantopsai-scheduler` on prod).
- **No trades in days:** check candidate counts per cycle. If zero candidates, the universe is empty or screener filters are too tight. Lower `min_volume`, widen `min_price` / `max_price`, or check the Active Lessons panel for crisis-state pause.
- **Trades firing but losing:** see workflow 7d above.
- **Settings change doesn't take effect:** profile picks up settings on the next cycle (within 5 min). If the change involves a schema column, the migration runs on the next service restart.
- **Symbol is being skipped:** check the cooldown table (`recently_exited_symbols`) — recently sold names get a 7-day cooldown; wash-trade-flagged names get 30 days. Also check the deprecated_strategies table.
- **AI says it can't propose options:** check that the candidate has `options_oracle_summary` (Alpaca options data). Crypto and some illiquid names don't.

## 10. Reference: which doc answers which question

- **What is this?** `01_EXECUTIVE_SUMMARY.md`.
- **How does the AI actually work?** `02_AI_SYSTEM.md`.
- **What does it trade and how does it size?** `03_TRADING_STRATEGY.md`.
- **What's the column / signal / feature called?** `05_DATA_DICTIONARY.md`.
- **What's the kill switch / hard gate?** `08_RISK_CONTROLS.md`.
- **What does this term mean?** `09_GLOSSARY.md`.

## See also

- `docs/03_TRADING_STRATEGY.md` for the strategy and risk philosophy that informs the settings.
- `docs/08_RISK_CONTROLS.md` for the risk-control toggles in detail.
- `OPEN_ITEMS.md` for what's still pending.
