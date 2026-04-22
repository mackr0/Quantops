# Self-Tuning System

QuantOpsAI's self-tuning system is an automated feedback loop that adjusts trading parameters based on the AI's own prediction accuracy. It runs daily at 3:55 PM ET — once per profile — and logs every decision it makes, whether it changes something or not.

**Status (2026-04-22):** All 10 active profiles have sufficient data and are actively tuning.

---

## How It Works

### What Is a "Resolved Prediction"?

Every time the AI evaluates a stock candidate (whether it trades it or not), a **prediction** is recorded with the AI's signal (BUY/SELL/HOLD), confidence score, price, and the full feature context. This is NOT the same as a closed trade — the AI makes predictions on every candidate in the shortlist, not just the ones it buys.

A prediction **resolves** when one of these conditions is met:

| Signal | Win Condition | Loss Condition | Timeout |
|--------|--------------|----------------|---------|
| BUY | Price rises >= 2% | Price drops >= 2% | 10 trading days |
| SELL | Price drops >= 2% | Price rises >= 2% | 10 trading days |
| HOLD | Price stays within +/-2% for 3 trading days | Price moves > 2% within 3 trading days | 10 trading days |

After 10 trading days with no threshold hit, the prediction resolves as "neutral."

**Why this matters:** A closed trade and a resolved prediction are different things. The AI might predict HOLD on 20 stocks and BUY on 2. All 22 are tracked as predictions, but only the 2 BUYs become trades. The self-tuner learns from ALL predictions — not just the ones that led to trades — because the AI's accuracy on HOLD calls matters just as much (a false HOLD on a stock that jumped 10% is a missed opportunity the tuner should learn from).

### The 20-Prediction Threshold

The self-tuner requires **at least 20 resolved predictions** before it will adjust anything. This prevents premature optimization on tiny sample sizes that could be pure noise.

### What It Adjusts (4 Parameters)

The self-tuner currently manages 4 parameters per profile:

#### 1. AI Confidence Threshold (`ai_confidence_threshold`)

Controls the minimum confidence score the AI must have before executing a trade.

- **Trigger:** If the win rate on predictions below 60% confidence is under 35% (on 5+ samples), raise the threshold to 60
- **Escalation:** If the win rate below 70% confidence is also under 35%, raise to 70
- **Effect:** The AI stops executing low-conviction trades that are statistically losing money

#### 2. Position Size (`max_position_pct`)

Controls what percentage of the portfolio each trade can use.

- **Trigger:** If overall win rate drops below 30%
- **Action:** Reduce position size by 20% (floor at 3%)
- **Effect:** Smaller bets while the AI is underperforming = less capital at risk

#### 3. Stop-Loss (`stop_loss_pct`)

Currently adjusted indirectly via ATR-based stops. Future self-tuning expansion (planned for late May 2026) will directly tune the ATR multiplier.

#### 4. Short Stop-Loss (`short_stop_loss_pct`)

- **Trigger:** If short selling has 0% win rate across 5+ trades
- **Action:** Widen the short stop-loss by 50% (cap at 20%)
- **Effect:** Gives short positions more room to work before stopping out
- **Escalation:** If 10+ short trades with < 20% win rate and negative total P&L, recommends disabling shorts entirely

### Safety Mechanisms

The self-tuner has several guardrails to prevent runaway adjustments:

#### 3-Day Cooldown

After any adjustment, that same parameter cannot be changed again for 3 days. This prevents rapid oscillation (e.g., raising and lowering the confidence threshold every day based on the latest batch of predictions).

#### Automatic Reversal

Every adjustment is reviewed after 3 days (with at least 10 new predictions since the change):

- **Improved:** The adjustment helped (win rate went up). Mark it as "improved" and keep it.
- **Worsened:** The adjustment hurt (win rate went down). Automatically reverse it to the previous value.
- **Neutral:** No meaningful change. Keep the adjustment but don't count it as a success.

This means the system can undo its own mistakes. If raising the confidence threshold to 70 causes the AI to miss good trades and the win rate drops, the tuner will set it back to what it was.

#### History Check

Before making any adjustment, the tuner checks if the same type of change was tried before and worsened performance. If a previous confidence threshold increase was reversed as "worsened," the tuner won't try it again.

#### Cross-Profile Learning

If another profile has a 20%+ higher win rate, the tuner logs a suggestion to adopt that profile's settings — but does NOT auto-apply it. Cross-profile changes are recommendations only, visible in the activity log and dashboard.

---

## What Gets Logged

Every self-tuning run produces a record, whether or not it makes changes:

### When Changes Are Made

Stored in the `tuning_history` table (in `quantopsai.db`):

| Field | Description |
|-------|-------------|
| `profile_id` | Which profile was tuned |
| `change_type` | Category (confidence_threshold, position_size, short_stop_loss, auto_reversal) |
| `parameter_name` | Exact DB column changed |
| `old_value` | Value before the change |
| `new_value` | Value after the change |
| `reason` | Human-readable explanation (e.g., "Win rate at <60% confidence was 28% (7/25)") |
| `win_rate_at_change` | Overall win rate when the decision was made |
| `predictions_resolved` | How many resolved predictions informed the decision |
| `timestamp` | When it happened |
| `outcome_after` | Filled in 3 days later: "improved", "worsened", or "neutral" |

### When No Changes Are Made

An activity log entry is created:

> **Self-Tuning: evaluated, no changes needed**
> Tuner reviewed 147 resolved AI predictions and found no parameters worth adjusting — current settings are performing within acceptable bounds.

This shows the tuner is running even when it has nothing to change.

---

## Viewing Self-Tuning Activity

### Performance Dashboard (AI Intelligence Tab)

- **Self-Tuning Readiness** table: shows each profile's resolved prediction count vs. the 20 required, with a progress bar
- **Tuning History** section: shows the last 10 adjustments across all profiles, with parameter, old/new values, reason, and outcome

### Activity Feed

Every tuning run creates an activity entry visible in the dashboard's activity feed, tagged as `self_tune`.

---

## Upward Optimization (Active)

The self-tuner doesn't just prevent disasters — it actively seeks higher win rates. When a profile has a win rate of 35%+ and 20+ resolved predictions, the upward optimizer kicks in alongside the safety-net logic.

**Key principle:** One change per run. The optimizer evaluates 5 strategies in priority order and applies only the first one that triggers. This lets the auto-reversal system attribute any win-rate change to that specific adjustment after 3 days.

### Strategy 1: Confidence Threshold Optimization

Analyzes win rate by confidence band (50-59, 60-69, 70-79, 80+). If a higher band has 10+ percentage points better win rate than overall, raises the threshold one band at a time. The AI then only executes trades where its confidence is strongest.

**Example:** If trades at confidence 70+ win 72% but overall is 50%, raises threshold from 25 to 50 (one band per run). Next run could raise to 60, then 70.

### Strategy 2: Regime-Aware Position Sizing

Compares win rate in the current market regime (bull/bear/sideways/volatile) against overall. If the current regime underperforms by 15+ points, reduces position size by 25%. If it outperforms by 15+ points, increases by 15%.

**Example:** In a sideways market with 27% win rate vs 47% overall, cuts position size from 10% to 7.5% to limit exposure until regime shifts.

### Strategy 3: Strategy Toggle Optimization

Identifies strategies with win rate below 30% AND 15+ points below overall. Disables the worst-performing one. Never disables the last remaining strategy.

**Example:** If Gap Reversal wins only 20% while overall is 50%, disables it so the AI focuses on strategies that are working.

### Strategy 4: Stop-Loss / Take-Profit Optimization

Analyzes the actual P&L distribution of closed trades:
- **Stop too tight:** If 40%+ of losses cluster within 1% of the stop-loss level, widens by 20% (giving trades more room to recover)
- **Take-profit too ambitious:** If the average winning trade captures less than 50% of the TP target, tightens by 20% (capturing more gains instead of waiting for unrealistic targets)

### Strategy 5: Position Size Increase

When there is a proven edge (55%+ win rate, 30+ predictions, positive average return), increases position size by 15% to capitalize. Hard cap at 15% per position.

### How These Interact with Safety Mechanisms

Every upward optimization uses the same safety infrastructure as the disaster-prevention logic:
- **3-day cooldown** per parameter — prevents rapid oscillation
- **Auto-reversal** — if a change worsens win rate after 3 days, it's automatically reversed
- **History check** — won't repeat a change that was previously reversed
- **Priority order** — only one change per run for clean attribution
- **Gate** — disabled entirely when win rate < 35% (disaster prevention takes over)

---

## Future Parameters (Planned Late May 2026)

After the current 4 parameters have 2-3 weeks of stable track record, three additional parameters will be added:

1. **Trailing Stop ATR Multiplier** — How tight/loose trailing stops are relative to volatility. Data is already being captured in `features_json` (ATR values) and the `trades` table (stop/take-profit prices).

2. **RSI Entry Thresholds** — Overbought/oversold cutoffs for entry signals. RSI is stored in every prediction's `features_json` as one of the 33 technical indicators.

3. **Volume Surge Multiplier** — Minimum volume ratio to confirm breakout signals. Volume ratio is captured in the candidate screening data stored in `features_json`.

All three data sources are already being collected with every scan cycle. When implemented, the self-tuner will calibrate from the full historical backlog — it will not start from zero.

---

## How This Differs from the Meta-Model (Phase 1)

The **self-tuner** adjusts trading parameters (confidence threshold, position size, stop-loss) based on aggregate win/loss statistics.

The **meta-model** (Phase 1 of the Quant Fund Evolution) is a gradient-boosted classifier that predicts "will this specific prediction be correct?" based on the full feature vector. It operates at the individual trade level, not the parameter level.

They complement each other:
- The self-tuner says "stop taking trades below 60% confidence" (macro adjustment)
- The meta-model says "this specific 75% confidence trade on AAPL in a bear market with RSI 80 is likely wrong" (micro prediction)

The meta-model will train automatically once any profile accumulates 100+ resolved predictions with feature data (~2-4 weeks from now).

---

## Technical Reference

| File | Purpose |
|------|---------|
| `self_tuning.py` | Core logic: `apply_auto_adjustments()`, `describe_tuning_state()`, `_analyze_failure_patterns()` |
| `multi_scheduler.py` | Scheduler integration: `_task_self_tune(ctx)` fires daily at snapshot time (3:55 PM ET) |
| `models.py` | `tuning_history` table CRUD: `log_tuning_change()`, `review_past_adjustments()`, `get_tuning_history()` |
| `views.py` | Dashboard display: `tuning_status` list built from `describe_tuning_state()` per profile |
| `templates/performance.html` | UI: Self-Tuning Readiness table, Tuning History section |
