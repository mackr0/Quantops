# QuantOpsAI Intelligence Upgrade — 8 Features

## Status Tracker

| # | Feature | Status | Phase |
|---|---|---|---|
| 1 | Drawdown Protection | DONE | A |
| 2 | Per-Stock Memory | DONE | A |
| 3 | Market Regime Detection | DONE | B |
| 4 | Cross-Profile Learning | DONE | C |
| 5 | Earnings Calendar Awareness | DONE | B |
| 6 | Multi-Model Consensus | DONE | C |
| 7 | Time-of-Day Patterns | DONE | B |
| 8 | Per-Stock Win Rate in Prompt | DONE | A |

## Build Order
- **Phase A (immediate):** Features 1, 2, 8 — stop bleeding, stop repeating mistakes
- **Phase B (market intelligence):** Features 3, 5, 7 — understand market conditions
- **Phase C (advanced):** Features 4, 6 — share knowledge, reduce single-model risk

---

## Feature 1: Drawdown Protection
**Problem:** System keeps trading at full size while bleeding money.
**Solution:** Track peak equity. If drawdown > 10%, reduce position sizes 50%. If > 20%, pause trading. Auto-resume at 5%.

**Files to modify:**
- `portfolio_manager.py` — add `check_drawdown(ctx, account_info)` reads peak from daily_snapshots
- `aggressive_trader.py` — check drawdown before executing, scale position size
- `user_context.py` — add `drawdown_pause_pct: float = 0.20`, `drawdown_reduce_pct: float = 0.10`
- `models.py` — add columns to trading_profiles
- `templates/settings.html` — drawdown threshold sliders in Risk Parameters

---

## Feature 2: Per-Stock Memory
**Problem:** System buys RIG repeatedly, loses every time.
**Solution:** Track win/loss per symbol. Auto-blacklist symbols with 0% win rate after 3+ predictions.

**Files to modify:**
- `self_tuning.py` — add `_build_symbol_reputation(db_path)` queries ai_predictions by symbol
- `aggressive_trader.py` — check if symbol has 0% win rate with 3+ resolved → skip "auto-blacklisted"
- `models.py` — add `auto_blacklist TEXT NOT NULL DEFAULT '[]'` to trading_profiles

---

## Feature 3: Market Regime Detection
**Problem:** Same strategy in bull, bear, sideways markets.
**Solution:** Analyze SPY/QQQ/VIX to classify regime. Shift strategy weights per regime.

**Files to create:**
- `market_regime.py` — `detect_regime()` returns regime, vix, trend, breadth, volatility, recommendation

**Files to modify:**
- `ai_analyst.py` — inject regime context into prompt
- `aggressive_trader.py` — fetch regime once per cycle, pass to AI
- `aggressive_strategy.py` — regime-based strategy weight adjustment

---

## Feature 4: Cross-Profile Learning
**Problem:** Mid Cap 64% win rate, MicroSmall 31%. No knowledge sharing.
**Solution:** Compare profiles, suggest underperformers adopt winning profile's settings.

**Files to modify:**
- `self_tuning.py` — add `_build_cross_profile_insights(user_id)` compares all profiles
- `self_tuning.py` — inject cross-profile context into performance prompt
- `self_tuning.py` — in `apply_auto_adjustments()` recommend adopting winning settings

---

## Feature 5: Earnings Calendar Awareness
**Problem:** Buying before earnings is gambling.
**Solution:** Check yfinance earnings calendar. Skip or flag stocks with earnings in 2 days.

**Files to create:**
- `earnings_calendar.py` — `check_earnings(symbol)`, `get_earnings_context(symbol)`, cached 24h

**Files to modify:**
- `ai_analyst.py` — inject earnings context per symbol
- `aggressive_trader.py` — optionally skip stocks near earnings
- `user_context.py` — add `avoid_earnings_days: int = 2`

---

## Feature 6: Multi-Model Consensus
**Problem:** Single AI opinion. Systematic bias affects every trade.
**Solution:** For STRONG signals, run second cheap model. Only trade when both agree.

**Files to modify:**
- `ai_analyst.py` — add `analyze_symbol_consensus()` runs primary + secondary model
- `aggressive_trader.py` — use consensus for AI review
- `user_context.py` — add `enable_consensus: bool = False`, `consensus_model: str = ""`
- `ai_providers.py` — add `get_cheapest_model(exclude_provider)`
- `templates/settings.html` — consensus checkbox + secondary model selector

---

## Feature 7: Time-of-Day Patterns
**Problem:** Markets behave differently at open vs midday vs close.
**Solution:** Track win rate by hour. Inject time context. Optionally skip opening volatility.

**Files to modify:**
- `self_tuning.py` — add `_build_time_context(db_path)` queries by hour
- `ai_analyst.py` — add current time to prompt
- `user_context.py` — add `skip_first_minutes: int = 0`
- `multi_scheduler.py` — delay first scan if skip_first_minutes > 0

---

## Feature 8: Per-Stock Win Rate in Prompt
**Problem:** AI sees overall stats but not stock-specific track record.
**Solution:** Enhance existing symbol parameter in build_performance_context.

**Files to modify:**
- `self_tuning.py` — enhance symbol-specific section, include last 3 predictions with outcomes
- `ai_analyst.py` — ensure symbol always passed to build_performance_context()
