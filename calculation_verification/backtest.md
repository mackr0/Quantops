# /backtest and /backtest-history — Calculation Register

Sources: `backtester.py` (`backtest_strategy`,
`backtest_with_params`, `backtest_comparison`), routes
`views.py run_backtest` (~7633) and the history route (~7584),
`backtest_worker.py`, templates `backtest.html`,
`backtest_history.html`, plus the settings-panel comparison consumer.
Verification pass: 2026-08-13 at `70ffbe2`.

---

## /backtest (quick simulator)

**Period / symbols tested / skipped** — days clamped [30, 365];
tested = symbols passing the 70-bar history gate; skipped =
insufficient history/warmup. **VERIFIED (code)**

**Total return / final equity** — equity accumulates
`pnl × (initial_capital × 10% / entry_price)` per closed trade —
fixed-fraction sizing on ORIGINAL capital, non-compounding. *Why:*
isolates signal quality from compounding effects. Part of **S16**:
the page nowhere says the model is non-compounding, and the number
reads like a portfolio return.

**Win rate / trade count** — `pnl > 0` wins over all trades
(break-even counts as loss — a third definition of win rate in the
system; folded into S12's definitional cleanup). **VERIFIED (code)**

**Sharpe** — mean/σ×√252 over the equity series' pct-changes; the
series appends per trade-event, not per calendar day — so "√252
annualization" is applied to a non-daily grid. Part of **S16**.

**Max drawdown / trade stats / stop-vs-target exit counts / best-
worst / trade table** — standard peak-trough on the same series;
ATR(14)-derived stops (2×) and targets (3×) with fixed fallbacks;
exit price is the LEVEL, not the bar price; NO slippage in this
engine. **VERIFIED (code)**, economics noted in S16.

**SUSPECT — S16 (surface-level):** `/backtest` runs a RANDOM
30-symbol sample per click, unseeded — two identical clicks give
different results, and results are presented without the sampling
caveat. Additionally this engine differs from the comparison engine
(below) in slippage (none vs modeled), trailing stops (absent vs
present), and sizing — cross-surface return figures are not
comparable, and nothing on either page says so.

## /backtest-history (parameter-comparison runs)

**Current/proposed returns** — `backtest_with_params` over the FULL
augmented universe (no sampling), WITH modeled slippage (per-side
`estimate_slippage`, 20 bps flat fallback) and trailing stops.
**VERIFIED (code)**

**Δ column** — recomputed template-side (`prop − cur`) while a stored
`diff` dict exists in the payload — two sources for one number
(currently equal; rounding drift possible). Folded into S16.

**Changes list** — formatted param diffs; note the float<1 branch is
checked before the bool branch, so boolean params stored as 0.0/1.0
would render as percentages — noted, current params avoid it.

**Settings comparison table** — same payload, JS `|| 0` defaults
(an unavailable metric renders as 0 — violates README convention 5;
folded into S16 as a rendering nit).

---

## Open items for this page

| ID | Item | Concern |
|---|---|---|
| S16 | Backtest surfaces | unseeded 30-symbol sampling presented without caveat; two engines with different economics and no cross-reference; non-compounding sizing unlabeled; √252 on non-daily grid; template-side Δ duplicates stored diff; JS zero-defaults |
