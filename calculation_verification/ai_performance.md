# /ai-performance — Calculation Register

**Routing fact first:** `/ai-performance` REDIRECTS to `/performance`
(no calculations of its own). The template `ai_performance.html` is
served only at **`/ai-performance-legacy`** (`views.py
ai_performance_legacy`, ~2891–3137). This register covers that legacy
page — and its headline finding is that the page has partially
rotted and should be retired or repaired (S10).

Verification pass: 2026-08-13 at `70ffbe2`.

---

## Actual trade results

**Trade win rate / W / L** — `wins/total × 100` over closed rows
(data-quality-excluded). **SUSPECT — S12:** the SQL counts `pnl <= 0`
as LOSING — break-even trades count as losses here, while
/performance classifies ±0.5% as scratch and excludes them from the
denominator. Same words ("win rate"), different definitions, on two
pages.

**Realized P&L / closed trades / avg per trade / best / worst** —
straight per-DB sums, cross-profile recompute for the average,
running max/min for extremes. **VERIFIED (code)**

## Slippage (stocks & options)

Same canonical `get_slippage_stats` chain as /ai (signed cost,
unsigned magnitude, fill-data counts, excluded-row disclosure), plus
an independent cross-DB re-query for the average. **VERIFIED (code)**

## Backtest vs reality

30-day simulated-vs-actual comparison (win rate, total return,
slippage, trade counts) from the dedicated API. **VERIFIED (code)** —
deeper validation of the simulator itself belongs to
[backtest.md](backtest.md).

## Risk analysis

Snapshot-preferred max drawdown (trade-curve fallback), worst
trade/day, streak walk (zero-pnl neutral), avg losing streak.
**VERIFIED (code)** — with one method note: this page's VaR uses the
index-floor percentile on **per-leg, decision-price** returns — the
pre-2026-08-13 method. /performance now uses fill-true, episode-
grouped returns with interpolated percentiles. Covered by **S15**:
the legacy page's risk block never received the option-basis fixes.

## Monthly returns

Month buckets by close date; wins/losses/scratch with `pnl == 0` as
scratch (definition differs from both /performance and this page's
own headline win rate — folded into S12); month return =
`pnl / first snapshot equity of the month`, honest "—" when no
baseline. **VERIFIED (code)**, definitions flagged.

## AI prediction accuracy

Counts, blended win rate (HOLDs included — unlike /ai's directional
split), profit factor over sign-consistent resolved rows, confidence
means, avg move on BUYs/SELLs (SELL match excludes SHORT here,
includes it on /ai). All real but subtly DIFFERENT from /ai's
definitions. Folded into **S10/S12** — two pages answering the same
questions with different populations.

## Dead sections (render permanently empty) — **SUSPECT S10**

- **Accuracy by confidence band** — the combined dict initializes
  `accuracy_by_confidence = {}` and no code ever merges the per-DB
  bands: the section always shows its empty state.
- **Best/Worst Trade cards and HOLD extremes** — the view aggregates
  `best_prediction`/`worst_prediction` but the template reads
  `best_trade`/`worst_trade`/`biggest_missed_gain`/
  `biggest_avoided_loss`, which are never set: all four cards always
  render "No closed trades yet."

## Self-tuning history

Same store as /ai's operations tab, formatted values, 20-row cap.
**VERIFIED (code)**

---

## Open items for this page

| ID | Item | Concern |
|---|---|---|
| S10 | Legacy page state | dead sections + duplicated-but-different definitions — retire or repair |
| S12 | Win/loss/scratch definitions | three different treatments of pnl==0 across pages |
| S15 | Legacy risk block | still uses pre-fix per-leg decision-price returns |
