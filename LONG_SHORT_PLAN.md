# Long/Short System Build — Plan

Started 2026-04-28. Goal: turn the system from "long pipeline with
shorts as a side door" into a real long/short trading system with
parity between directions and genuine alpha sources beyond
technicals. Modeled on what real long/short equity hedge funds
actually do (Citadel, Millennium, Point72) — not just
"buying with the sign flipped."

## Why this matters

Today the system has:
- A working long pipeline (69.9% trade win rate, +$20K realized P&L)
- "Shorts allowed if `enable_short_selling=1`" as an afterthought
  — same screener, same sizing, same stops, same self-tuning bucket
- Net result on the dedicated "Small Cap Shorts" profile (10):
  **2 SHORT predictions in 1,491 prediction rows**

That's not a short capability. It's a long pipeline that occasionally
emits a SHORT because nothing blocks it.

Real long/short funds are where stock-picking alpha lives — Sharpe
ratios of 2–3 vs 0.5–1 for long-only over decades. Long-only is
where the AUM is but not where the edge is. Skipping shorts is
skipping the table when your goal is "compete with real funds."

## Phase structure

Three phases. Each is buildable; each delivers value if we stop there.

- **Phase 1 — Short capability with parity to longs.** Table stakes.
- **Phase 2 — Pair / sector-neutral / factor-aware construction.**
  Where the high-Sharpe quant funds live.
- **Phase 3 — Real alpha sources beyond technicals.** Earnings,
  insider, catalyst-driven. Ongoing.

## Phase 1 — Short capability (parity with longs)

Estimated ~1-2 weeks of focused work. Each subtask is a separate
commit; nothing deploys until the dependent piece is verified.

### 1.0 — Foundation: SELL semantic fix (DO FIRST)

**Why first:** every subsequent metric, self-tuning bucket, and
backtest depends on knowing what a row in `ai_predictions` means.
Today SELL conflates "exit a long position" and "predict a price
drop." The resolver labels both the same way, which means the data
underneath everything else is dirty.

**Build:**
- Add `prediction_type` column to `ai_predictions`:
  `'directional_long' | 'directional_short' | 'exit_long' | 'exit_short'`
- At write time in `trade_pipeline.py`, classify based on
  `(predicted_signal, do-we-currently-hold-this-symbol)`:
  - BUY + not held → `directional_long`
  - BUY + held → ignored (we don't double-buy)
  - SHORT + not held → `directional_short`
  - SELL + held long → `exit_long`
  - SELL + not held → `directional_short` (rare; AI hallucinated SELL)
  - SELL + held short → `exit_short`
  - HOLD → `directional_long` (saying "price will be range-bound")
- Update `ai_tracker._resolve_one`:
  - `directional_long`: existing BUY logic
  - `directional_short`: existing SELL logic (price drop = win)
  - `exit_long`: did the price keep going up after exit (left money on
    table) or did it actually decline (good exit). Win = price within
    ±2% of exit price OR price declined. Loss = price kept rising
    materially.
  - `exit_short`: mirror of exit_long
- Backfill ~5,900 existing resolved rows by inspecting position state
  at prediction timestamp.
- Dashboard: split "Avg Move on SELLs" into separate metrics — one
  per `prediction_type`.

**Done when:** the dashboard shows separate accuracy for "directional
shorts" vs "exit timing" and the underlying database has clean
classifications.

### 1.1 — Dedicated bearish strategies

**Why:** today's "bearish strategies" are mostly bullish strategies
with a sign flip. Real shorts require pattern detectors built for
short setups specifically.

**Build five new strategy modules:**

- `breakdown_support` — symbol breaks below 20-day or 50-day swing
  low on >1.5× avg volume. Closes below the support line.
- `distribution_at_highs` — rising volume on red days while price
  flatlines at 52-week highs. Detects smart-money exit before the
  obvious breakdown.
- `failed_breakout` — long traps. Symbol broke above resistance in
  prior 5 days, then closed back below. High volume on the failure.
- `parabolic_exhaustion` — extreme RSI (>85) + recent +20% in <10
  days + volume spike + reversal candle (bearish engulfing /
  shooting star).
- `relative_weakness_in_strong_sector` — sector ETF up >2% in 5d
  but stock down — fundamental issue likely, sector tailwind not
  enough to lift it.

Each module follows the existing strategy pattern (`find_candidates`,
`NAME`, `APPLICABLE_MARKETS`). Register in `strategies/__init__.py`.

**Done when:** each module emits SELL/SHORT candidates on appropriate
real-data scans, and a dry-run shows the new bearish-candidate count
goes from ~10/cycle (current) to ~30-50/cycle on shorts-enabled
profiles.

### 1.2 — Borrow / HTB filter

**Why:** Alpaca paper fills any short order. Live brokers refuse
hard-to-borrow names or charge 50%+ annual borrow. We currently
have no check.

**Build:**
- Add `_check_borrow_available(symbol, ctx)` in `client.py` that
  queries Alpaca's asset endpoint for `easy_to_borrow` and
  `shortable` fields. Cache 24h.
- Wire into `_rank_candidates` in `trade_pipeline.py`: when a
  candidate is SHORT/SELL, skip it if not borrowable.
- Surface skip count in pipeline summary so we can see how many
  good shorts are filtered out by borrow constraints.

**Done when:** SHORT candidates with `shortable=False` never reach
the AI, and the pipeline log shows e.g. "5 SHORT candidates filtered
for borrow availability".

### 1.3 — Squeeze-risk filter

**Why:** short interest >20% + low float + meme history = squeeze
risk. One squeeze can wipe out months of gains.

**Build:**
- Use yfinance/alt source for `short_interest_ratio` and
  `shares_float`. Cache 24h.
- Add `_squeeze_risk_score(symbol)`:
  - HIGH if SI% > 20% OR float < 50M shares
  - MED if SI% 10-20% OR float 50-100M
  - LOW otherwise
- Skip HIGH squeeze risk on SHORT candidates. Allow MED with
  reduced position size.

**Done when:** verifiably-squeezable names (look at recent meme
list) get filtered with a logged reason.

### 1.4 — Regime gate for shorts

**Why:** shorting in a strong bull market is hard mode. Long edge
works secularly; short edge needs neutral-or-bear regime OR
stock-specific catalyst.

**Build:**
- Use existing `crisis_state` infrastructure plus add a
  `market_regime` classifier (SPY > 200d MA + rising = strong_bull,
  else neutral, etc.)
- In `_rank_candidates`, when current regime is `strong_bull` AND
  the candidate doesn't have a catalyst (earnings, insider, news
  flag), skip it. Catalyst shorts can still go through.

**Done when:** shorts on routine technical breakdowns are
suppressed in strong bull regimes; catalyst shorts continue.

### 1.5 — Time stops on shorts

**Why:** shorts that don't move down quickly should be covered.
Borrow keeps eating capital and the premise was probably wrong.
Longs can drift — shorts cannot afford to.

**Build:**
- Add `short_max_hold_days` to UserContext (default 10).
- In `check_exits`, when iterating short positions, cover any
  position older than `short_max_hold_days` regardless of P&L.
  Log reason "time stop".

**Done when:** short positions auto-cover after N days even if
neither TP nor SL was hit.

### 1.6 — Asymmetric position sizing

**Why:** unlimited downside on shorts means smaller sizes. Pro
convention: shorts get half the size of longs.

**Build:**
- Add `short_max_position_pct` to UserContext (default = half of
  `max_position_pct`).
- In trade execution path, use the right pct based on direction.
- Self-tuner can adjust both independently in future tuning runs.

**Done when:** SHORT trades execute at the smaller size and
the journal reflects it.

### 1.7 — Two shortlists (long and short)

**Why:** today the rank function returns one merged top-15 list
sorted by abs(score). On shorts-enabled profiles, this crowds out
bearish candidates because most strategies emit bullish signals.

**Build:**
- Refactor `_rank_candidates` to return either:
  - `{"longs": [...], "shorts": []}` (shorts disabled), OR
  - `{"longs": [...top 10 long...], "shorts": [...top 5 short...]}`
    (shorts enabled — give shorts dedicated slots).
- Update the AI prompt to show both lists separately.

**Done when:** shorts-enabled profiles consistently send 5
short-candidates to the AI per cycle (vs 0-1 today).

### 1.8 — Updated AI prompt for long/short

**Why:** today's batch prompt is bullish-defaulted. "Pick the best
0-3 trades. Actions allowed: BUY | SHORT" naturally biases to
BUY because the candidates are pre-ranked bullish.

**Build:**
- Two-section prompt when shorts enabled: "LONG CANDIDATES" + "SHORT
  CANDIDATES"
- Explicit instruction: "Pick 0-3 trades from EITHER side. Don't
  feel obligated to pick longs — a strong short setup beats a
  mediocre long."
- Track per-direction conviction so the AI shows BUY confidence and
  SHORT confidence separately if it picks both.

**Done when:** shorts-enabled profiles emit SHORT actions roughly
proportional to short candidates seen (target: 20-30% of trades on
profile_10 should be SHORT, vs <1% today).

### 1.9 — Per-direction self-tuning

**Why:** self-tuner today learns from aggregate. A profile with 200
working longs and 5 random shorts will learn "the strategy works"
even if shorts are bleeding.

**Build:**
- In `self_tuning.py`, split the tuning history bucket by direction.
- Win rates, profit factors, and tuning decisions compute separately
  for long and short books.
- The tuner can disable longs OR shorts independently if one side
  underperforms while the other works.

**Done when:** `tuning_history` table shows separate long/short
buckets and the tuner respects them.

## Phase 2 — Pair / sector-neutral / factor-aware

Where the highest-Sharpe quant funds live.

### 2.1 — Sector exposure tracking
Compute current long_sector_exposure and short_sector_exposure per
profile. Surface in the Performance Dashboard.

### 2.2 — Long/short ratio targets
Add `target_short_ratio` to UserContext. Profile_10 might aim for
70% short / 30% long during bear regimes, 50/50 in neutral, 30/70
in bull.

### 2.3 — Pair trades primitive
When AI sees strong long + strong short in same sector, propose
paired trade (long winner + short loser). Lower beta, isolates the
relative-strength signal.

### 2.4 — Net-exposure rebalancing
Daily task that checks current net exposure vs target and either
trims longs or covers shorts to bring back in line.

### 2.5 — Factor-neutral construction
For each candidate compute factor exposures (size, value, momentum).
Try to keep portfolio factor-neutral.

## Phase 3 — Real alpha sources

Ongoing. Each is a discrete strategy module.

### 3.1 — Earnings-disaster short pattern
Companies that miss + guide down + gap down typically continue
declining for 60-90 days (Bernard & Thomas 1990 PEAD effect, but
inverted). New strategy.

### 3.2 — Catalyst-driven shorts
Hook into existing event_detectors for: SEC filings of fraud,
downgrades after pumps, sector breakdowns. Generate SHORT candidates
on event triggers.

### 3.3 — Sector rotation overlay
When sector_momentum_rotation phase is "early bear" or "late bull",
shift allocation: long defensive sectors, short formerly-leading.

### 3.4 — Volatility regime trades
High IV-rank names mean-revert. Shorts when IV rank > 90 with
bearish technicals. Already partially covered by
`high_iv_rank_fade`; expand and refine.

### 3.5 — Insider signal weighting
`insider_cluster` and `insider_selling_cluster` have documented
edge (Seyhun 1986, Cohen et al. 2012). Today they're treated as
secondary signals. Promote to primary on a dedicated insider-weight
profile.

## Risks and what could go wrong

- **Phase 1 changes touch the core decision pipeline.** Risk of
  breaking long-side decisions. Mitigation: keep buy path unchanged
  when `enable_short_selling=False`; test extensively per commit.
- **Borrow / HTB data quality.** Alpaca paper might say "shortable"
  for names live brokers refuse. The metric is best-effort, not
  ground truth.
- **Squeeze-risk false positives.** Some legitimate shorts have high
  short interest because they ARE good shorts (everyone sees the
  weakness). Filter is a heuristic, not a wall.
- **Regime classification.** "Strong bull" vs "neutral" is a fuzzy
  call. Need to be reasonably stable — don't toggle daily.
- **Self-tuning split could starve.** If a profile takes 5 shorts in
  3 months, the per-direction tuner has no data to act on.
- **Phase 2 changes assume real diversification.** With 10 positions
  total and 8 sectors, sector-neutrality is a stretch. Build
  conservatively.

## What "done" looks like overall

- Profile_10 "Small Cap Shorts" has SHORT/SELL_EXIT actions on
  20-30% of its trades, not <1%
- Shorts have their own measured slippage, win rate, profit factor,
  and self-tuning bucket on the dashboard
- The system can detect breakdown / exhaustion / failed-breakout
  patterns natively, not just sign-flipped bullish patterns
- Catalyst-driven shorts (earnings, insider, downgrades) hit the
  AI prompt with appropriate priority
- Net exposure is a tracked variable, not an emergent property
- We can honestly say: "the system is long/short; it makes money
  in bull markets via the long book, in bear markets via the short
  book, in neutral markets via the relative-strength pair trades."

## Order of execution

1. Phase 1.0 (SELL semantic fix) — foundation for clean data
2. Phase 1.1 (bearish strategies) — supply of real short candidates
3. Phase 1.6 + 1.5 (sizing + time stops) — cheap, foundational
4. Phase 1.7 + 1.8 (two shortlists + prompt) — wire new strategies
5. Phase 1.2 + 1.3 + 1.4 (borrow + squeeze + regime filters)
6. Phase 1.9 (per-direction tuning) — closes the feedback loop
7. Phase 2.x (pair / sector neutrality)
8. Phase 3.x (real alpha sources, ongoing)
