# QuantOpsAI Multi-User Platform — Architecture Plan

## Why This Change

You want your friends to use QuantOpsAI with their own Alpaca paper trading accounts. Each person needs their own isolated account, their own strategy settings, and a web UI to customize everything — without being able to see or affect anyone else's trades.

---

## What Gets Built

A web-based frontend on top of the existing trading engine. Users log in, enter their Alpaca keys, configure strategy parameters with sliders and dropdowns, and the system trades for them autonomously.

```
                    ┌──────────────────────────────┐
                    │       Web UI (Flask)          │
                    │  Login / Dashboard / Settings │
                    └──────────────┬───────────────┘
                                   │
                    ┌──────────────▼───────────────┐
                    │     SQLite Database           │
                    │  users, configs, trades, AI   │
                    └──────────────┬───────────────┘
                                   │
        ┌──────────────────────────▼──────────────────────────┐
        │              Multi-User Scheduler                    │
        │  For each active user → for each enabled segment:    │
        │    Screen → Analyze → AI Review → Trade → Notify     │
        └─────────────────────────────────────────────────────┘
```

---

## Tech Stack

| Component | Choice | Why |
|---|---|---|
| **Web framework** | Flask | Lightweight (~15MB RAM), Jinja2 templates, fits on $6 droplet |
| **Frontend** | Server-rendered HTML + vanilla JS | No build step, no Node.js dependency |
| **CSS** | Pico CSS (single file) | Clean styling with zero configuration |
| **Auth** | Flask-Login + bcrypt | Simple email/password sessions |
| **Database** | Single SQLite (with user_id scoping) | No extra service, no RAM overhead |
| **Encryption** | Fernet (cryptography library) | API keys encrypted at rest |
| **Web server** | Gunicorn (2 workers) behind nginx | Production-ready, ~60MB RAM total |
| **Scheduler** | Existing multi_scheduler.py, extended | Single process, sequential user iteration |

---

## Anthropic API Key Strategy

**Cost per user per month: ~$5–14** (on the platform-provided key)

Each AI review call costs ~$0.0045. With 13 scan cycles/day × ~5-10 AI reviews per cycle × 21 trading days:

| Users | Est. Monthly Anthropic Cost |
|---|---|
| 1 (you) | $5–14 |
| 3 | $15–42 |
| 5 | $27–70 |
| 10+ | Require BYO key |

**Decision:** No shared platform key. Every user must provide their own Anthropic API key. This keeps your costs at zero and avoids hitting your limits from other projects. The settings page makes the Anthropic key a required field with a link to https://console.anthropic.com for sign-up.

---

## User Account Isolation

| What | How It's Isolated |
|---|---|
| **Alpaca credentials** | Encrypted per-user in database (Fernet). Decrypted only at runtime. |
| **Trades & positions** | Every DB query includes `WHERE user_id = ?` |
| **Strategy settings** | Stored per-user, per-segment in `user_segment_configs` table |
| **AI predictions** | Scoped by `user_id` — each user gets their own accuracy stats |
| **Email notifications** | Each user sets their own notification email |
| **Alpaca API client** | Created fresh per-user from their decrypted credentials |

There is no shared mutable state between users at runtime.

---

## Database Schema

### New Tables

**`users`**
| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | Auto-increment |
| email | TEXT UNIQUE | Login identifier |
| password_hash | TEXT | bcrypt hashed |
| display_name | TEXT | Optional friendly name |
| alpaca_api_key_enc | TEXT | Fernet-encrypted Alpaca key |
| alpaca_secret_key_enc | TEXT | Fernet-encrypted Alpaca secret |
| anthropic_api_key_enc | TEXT | Required — each user brings their own |
| notification_email | TEXT | Where to send trade alerts |
| resend_api_key_enc | TEXT | NULL = no email |
| is_active | INTEGER | 1 = trading enabled |
| is_admin | INTEGER | 1 = can see admin panel |
| created_at | TEXT | ISO timestamp |

**`user_segment_configs`** (one row per user per segment)
| Column | Type | Default |
|---|---|---|
| user_id | INTEGER FK | — |
| segment | TEXT | 'smallcap' / 'midcap' / 'largecap' |
| enabled | INTEGER | 0 |
| stop_loss_pct | REAL | Segment default |
| take_profit_pct | REAL | Segment default |
| max_position_pct | REAL | Segment default |
| max_total_positions | INTEGER | 10 |
| ai_confidence_threshold | INTEGER | 25 |
| min_price | REAL | Segment default |
| max_price | REAL | Segment default |
| min_volume | INTEGER | Segment default |
| volume_surge_multiplier | REAL | 2.0 |
| rsi_overbought | REAL | 85 |
| rsi_oversold | REAL | 25 |
| momentum_5d_gain | REAL | 3.0 |
| momentum_20d_gain | REAL | 5.0 |
| breakout_volume_threshold | REAL | 1.0 |
| gap_pct_threshold | REAL | 3.0 |
| strategy_momentum_breakout | INTEGER | 1 (on) |
| strategy_volume_spike | INTEGER | 1 (on) |
| strategy_mean_reversion | INTEGER | 1 (on) |
| strategy_gap_and_go | INTEGER | 1 (on) |
| custom_watchlist | TEXT | '[]' (JSON array) |

**`user_api_usage`** (daily API call tracking)
| Column | Type |
|---|---|
| user_id | INTEGER FK |
| date | TEXT |
| anthropic_calls | INTEGER |

**`decision_log`** (full audit trail for the expandable trade detail view)
| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | Auto-increment |
| user_id | INTEGER FK | — |
| segment | TEXT | — |
| timestamp | TEXT | When the decision was made |
| symbol | TEXT | — |
| decision_type | TEXT | 'trade_executed', 'ai_vetoed', 'exit_triggered', 'hold' |
| technical_score | INTEGER | Combined strategy score (-4 to +4) |
| strategy_votes | TEXT | JSON: {"momentum_breakout": "BUY", "volume_spike": "HOLD", ...} |
| strategy_reasons | TEXT | JSON: {"momentum_breakout": "reason...", ...} |
| ai_signal | TEXT | BUY/SELL/HOLD |
| ai_confidence | INTEGER | 0-100 |
| ai_reasoning | TEXT | Full AI explanation |
| ai_risk_factors | TEXT | JSON array of risk strings |
| ai_price_targets | TEXT | JSON: {"entry": X, "stop_loss": Y, "take_profit": Z} |
| veto_rule | TEXT | NULL if approved, or the specific rule that vetoed |
| action_taken | TEXT | 'BUY', 'SELL', 'VETOED', 'HOLD', 'SKIP' |
| qty | REAL | Shares traded (NULL if no trade) |
| price | REAL | Execution price |
| order_id | TEXT | Alpaca order ID |
| exit_trigger | TEXT | NULL, 'stop_loss', 'take_profit', 'strategy_sell' |
| pnl | REAL | Realized P&L (for exits) |

This table is the source for the expandable trade detail view. Every scan cycle writes one row per candidate that reached the AI review stage.

### Existing Tables Modified

`trades`, `signals`, `daily_snapshots`, `ai_predictions` all gain:
- `user_id INTEGER NOT NULL`
- `segment TEXT NOT NULL`

Existing data gets `user_id = 1` (admin/owner) during migration.

---

## Web UI Pages

### 1. Login (`/login`)
Email + password form.

### 2. Register (`/register`)
Email, password, display name. Creates account with all segments disabled.

### 3. Dashboard (`/dashboard`) — Main page after login (replaces Alpaca dashboard)
- **Account overview card**: Equity, cash, buying power, total P&L — live from Alpaca API
- **Open positions table** per enabled segment:
  - Symbol, qty, avg entry, current price, market value, unrealized P&L, P&L %
  - Color-coded green/red
  - Stop-loss and take-profit price levels shown per position
- **Today's activity feed** (chronological):
  - Trades executed (with expandable detail — see below)
  - AI vetoes (with expandable reasoning)
  - Stop-loss / take-profit exits (with trigger details)
- **AI performance summary**: Win rate, profit factor, total predictions
- **One card per enabled segment** with quick stats

Users never need to visit Alpaca — everything is visible here.

### 4. Settings (`/settings`) — Strategy customization
- **API Keys section** (top)
  - Alpaca Key + Secret (required)
  - "Test Connection" button
  - Anthropic Key (required — link to https://console.anthropic.com to get one)
  - Notification email + Resend key (optional)
- **Segment cards** with enable/disable checkbox each:
  - **Risk Management**
    - Stop-loss %: slider (1–20%, step 0.5)
    - Take-profit %: slider (1–50%, step 1)
    - Max position %: slider (1–25%, step 1)
    - Max positions: dropdown (1–20)
  - **AI Settings**
    - Confidence threshold: slider (0–100, step 5)
  - **Screener Settings**
    - Volume surge multiplier: slider (1.0–5.0, step 0.1)
    - RSI oversold: slider (5–40, step 1)
    - RSI overbought: slider (60–95, step 1)
    - Momentum 5d gain: slider (1–20%, step 0.5)
    - Momentum 20d gain: slider (1–30%, step 1)
    - Breakout volume threshold: slider (0.5–3.0, step 0.1)
    - Gap % threshold: slider (1–10%, step 0.5)
  - **Strategy Toggles** (checkboxes)
    - Momentum Breakout: on/off
    - Volume Spike: on/off
    - Mean Reversion: on/off
    - Gap and Go: on/off
  - **Custom Watchlist**
    - Text area for comma-separated symbols to add to the segment's universe
  - "Reset to Defaults" button per segment
- "Save All" button

### 5. Trades (`/trades`) — Full decision audit trail
Trade history with filtering by segment, date range, symbol.

**Each trade row is expandable.** Click to reveal the full decision chain:

```
┌─────────────────────────────────────────────────────────┐
│ BUY 1,145 WVE @ $6.53 — Mar 27, 10:32 AM              │
│ Segment: Small Cap | Strategy: aggressive               │
├─────────────────────────────────────────────────────────┤
│ SCREENING                                                │
│   Found via: Mean Reversion (RSI 21.9, -46.5% below SMA)│
│                                                          │
│ STRATEGY VOTES (Score: +1)                               │
│   ✓ Momentum Breakout:  HOLD — no breakout               │
│   ✓ Volume Spike:       HOLD — no volume trigger          │
│   ✓ Mean Reversion:     BUY  — RSI 21.9, -46.5% below   │
│   ✓ Gap and Go:         HOLD — no gap                     │
│                                                          │
│ AI REVIEW                                                │
│   Signal: BUY | Confidence: 65%                          │
│   Reasoning: "RSI deeply oversold at 21.9 with price     │
│   significantly below SMA20. MACD histogram showing      │
│   potential bullish divergence..."                        │
│   Risk Factors: sector weakness, low volume               │
│   Price Targets: entry $6.50, stop $6.30, target $7.15   │
│   Decision: ✅ APPROVED                                   │
│                                                          │
│ EXECUTION                                                │
│   Filled: 1,145 shares @ $6.53 | Cost: $7,476.85        │
│   Stop-loss: $6.33 (-3%) | Take-profit: $7.18 (+10%)    │
│                                                          │
│ OUTCOME (if closed)                                      │
│   Exit: Stop-loss triggered at $6.33 (-3.1%)             │
│   P&L: -$228.90                                          │
└─────────────────────────────────────────────────────────┘
```

**AI veto entries** show the same detail but with:
- What the technical analysis recommended
- Why the AI disagreed (full reasoning)
- The specific veto rule that fired

**Exit entries** show:
- Which trigger fired (stop-loss, take-profit, or strategy SELL signal)
- The exact price and % at trigger time
- Realized P&L

### 6. AI Performance (`/ai-performance`)
- Win rate overall and by signal type
- Accuracy by confidence band (visual chart)
- Best/worst predictions with full reasoning
- Profit factor
- Comparison: "trades AI approved" vs "trades AI vetoed" — what would have happened?

### 7. Admin (`/admin`) — Owner only
- User list with status
- Per-user API usage
- System health (scheduler status, last run times)
- Ability to disable users

---

## Scheduler Changes

### Current (single-owner)
```
for segment in [smallcap, midcap, largecap]:
    mutate config globals
    run_segment_cycle()
    restore config globals
```

### New (multi-user)
```
for user in get_active_users():
    ctx = build_user_context(user)  # from DB, decrypted
    for segment in user's enabled segments:
        segment_config = load_segment_config(user.id, segment)
        run_segment_cycle(ctx, segment_config)
```

The key refactor: **eliminate config mutation**. Instead, create a `UserContext` dataclass that carries all parameters and gets passed through the call chain. Every function that currently reads `config.ALPACA_API_KEY` etc. will instead receive the value from the context.

### Market Data Caching

All users trading the same segment see the same market data. The screener will cache yfinance batch downloads for 5 minutes. If user A already fetched smallcap data, user B reuses it. This cuts per-user scan time from ~15s to <1s for the second user onward.

---

## Droplet Resource Budget

| Component | RAM |
|---|---|
| Ubuntu OS + systemd | ~200 MB |
| Gunicorn (2 Flask workers) | ~60–80 MB |
| Scheduler process | ~80–120 MB |
| SQLite | ~5 MB |
| **Total** | **~345–405 MB** |
| **Free** | **~600–650 MB** |

**Verdict:** The $6 droplet handles up to ~5–8 users comfortably. Beyond that:
- $12/mo (2GB RAM) for 10–15 users
- $18/mo (2 vCPU, 2GB) if CPU becomes the bottleneck

---

## Implementation Phases

### Phase 1: Refactor Config → UserContext
- Create `UserContext` dataclass
- Update every function that reads `config.*` to accept parameters instead
- `multi_scheduler.py` builds UserContext from `segments.py` (preserves current behavior)
- **Test:** Your current 3-segment system works identically after refactor

### Phase 2: Database Migration
- Add `users`, `user_segment_configs`, `user_api_usage` tables
- Add `user_id` + `segment` columns to existing tables
- Create migration script that:
  - Creates your admin account
  - Imports your credentials from `.env`
  - Backfills `user_id = 1` on existing trades/signals/predictions
  - Creates your segment configs from current values

### Phase 3: Flask Web Application
- Login, register, dashboard, settings, trades, AI performance pages
- Settings page with all sliders and dropdowns
- API key encryption/decryption
- Read-only access to trade data from shared SQLite

### Phase 4: Multi-User Scheduler
- Query active users from DB instead of `.env`
- Load per-user configs from `user_segment_configs`
- Market data caching across users
- Per-user API usage tracking

### Phase 5: Deploy
- Install nginx as reverse proxy
- Add `quantopsai-web.service` (gunicorn)
- Update `quantopsai-scheduler.service` (multi-user scheduler)
- Run migration script
- Update `deploy.sh`

---

## What Your Friends Need To Do

1. You send them the URL (http://your-droplet-ip or a domain if you set one up)
2. They register with email + password
3. They go to Settings and enter their Alpaca paper trading API key + secret
4. They click "Test Connection" to verify
5. They enable whichever segments they want (smallcap, midcap, largecap)
6. They optionally customize strategy parameters with sliders
7. They click Save — the scheduler picks them up on the next cycle

That's it. They can check their dashboard anytime to see positions, trades, and AI performance. They'll get email notifications if they configure them.
