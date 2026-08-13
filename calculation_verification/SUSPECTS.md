# Suspects Register — calculations flagged for operator review

Every SUSPECT entry from the per-page registers, in one place. An item
leaves this list only by being **fixed** (fix noted, entry moved to the
Resolved section) or **accepted** (operator initials + rationale
recorded). Nothing ages out silently.

## Open

**S1 — Annualized-return window mismatch** (/performance)
`total_return` spans the daily-snapshot range; `days_active` spans
first→last TRADE. A profile that idles before its first trade or after
its last annualizes a return over the wrong day count.
*Proposed:* derive days_active from the snapshot range (same series as
the numerator), falling back to trade span only when snapshots are
absent.

**S2 — Sharpe: risk-free rate and gap-fill denominator** (/performance)
(a) Sharpe/Sortino use raw mean return — no risk-free subtraction. At
~5% cash rates this materially flatters both vs any external
comparison. (b) When consecutive snapshots are missing, the daily
return falls back to `daily_pnl / first_equity` — the account's
*original* equity — biasing the series once equity drifts.
*Proposed:* (a) subtract a configurable daily rf (default 13-week
T-bill, cached); label the cell "excess". (b) use the previous
available snapshot's equity as the denominator for gap days.

**S3 — Sortino uses std of negative days, not downside deviation**
(/performance) Textbook Sortino: `sqrt(Σ min(r,0)² / N)` over ALL N
observations. Current: `std(negatives only)`. Auditors recomputing per
the standard definition will not reproduce the page.
*Proposed:* switch to downside deviation; keep the old value one
release under a tooltip for continuity.

**S4 — Unit inconsistency: per-leg counts vs per-episode
distributions** (/performance, and Kelly inputs)
Win/loss/scratch counts, streaks, largest win/loss, expectancy, and
Kelly inputs are per closed ROW; VaR/CVaR and avg win/loss % are per
DECISION (spread episodes). Both are defensible; showing them side by
side without saying so is not.
*Proposed:* either move counts to episodes too, or label the two
groups explicitly on the page ("per fill-row" vs "per decision").
Operator call — affects how win rate reads historically.

**S5 — Benchmark bar series unverified against an independent source**
(/performance) Beta/alpha/correlations trust the fetched SPY/QQQ/BTC
daily series. A silently stale or partial series skews beta without
any error.
*Proposed:* one-time spot-check of 10 random days against a second
source; add a freshness assertion (last bar within 3 trading days)
that renders the cells "unavailable" when stale.


**S6 — Realized P&L policy split on data_quality rows** (/trades vs
/performance) The trades-page Realized sum includes
`data_quality`-tagged rows; the performance page's Total P&L excludes
them. The two pages can disagree by exactly the tagged rows' pnl.
*Proposed:* one policy — exclude everywhere, with the trades page
showing "N excluded rows" beside the sum.

**S7 — "{N} total" on /trades is post-limit** The result-set count is
taken after the 100/200-row fetch cap, so a busy profile displays the
cap as if it were the filtered total.
*Proposed:* real COUNT(*) per filter, or a "showing latest N" label.

**S8 — Dashboard AI-brain option trade lines render unpersisted
fields** The multileg/options branches read `contracts`,
`strategy_name`, `option_strategy` from the cycle snapshot, but the
writer persists only symbol/action/size_pct/confidence/reasoning —
option lines render with missing sizing.
*Proposed:* persist the three fields in `_save_cycle_data` (and
`ai_cycles.trades_selected_json`) or fall back to size_pct in the
renderer.


**S9 — /ai "Prediction Accuracy" tile displays a profit factor** The
label says accuracy; the number is Σ positive returns / |Σ negative|.
*Proposed:* rename the tile "Profit Factor (predictions)".

**S10 — /ai-performance-legacy has rotted** `/ai-performance`
redirects to /performance, but the legacy page remains reachable
with: a permanently-empty confidence-band section (dict never
merged), four extreme-trade cards that always render their empty
state (key mismatch), and definitions that differ from /ai for the
same questions. *Proposed:* retire the page, or repair and reconcile
definitions.

**S11 — HOLD pass-rate aggregation round-trips percentages**
Cross-profile totals reconstruct win counts from per-DB rounded
percentages. *Proposed:* return raw hold_wins from the tracker and
sum integers.

**S12 — Three definitions of win/loss/scratch across surfaces**
/performance: ±0.5% scratch band excluded from the denominator;
legacy page + journal summary + /backtest: pnl≤0 (or pnl>0) binary
with break-even a loss; legacy monthly table: pnl==0 scratch.
*Proposed:* one definition (the scratch-band one), applied
everywhere, with the band stated on each page.

**S13 — "99% VaR (Monte Carlo)" tile renders the 95% column**
`mc_var_95_dollars` feeds a tile labeled 99%; a real 99% column
exists unused. *Proposed:* fix whichever side is wrong.

**S14 — "What the AI Sees" counts are hard-coded prose** (15
indicators / 34 sources / 26 strategies …) and will drift.
*Proposed:* compute them, or label as illustrative.

**S15 — Legacy risk block predates the option-basis fixes** VaR on
/ai-performance-legacy still uses per-leg decision-price returns with
index-floor percentiles. *Proposed:* delete with S10 or route through
metrics/legacy's fixed machinery.

**S16 — Backtest surfaces: sampling, economics, and labeling**
/backtest runs an UNSEEDED random 30-symbol sample (two clicks, two
answers) presented without caveat; its engine has no slippage and no
trailing stops while the comparison engine has both; sizing is
non-compounding fixed-fraction but reads like a portfolio return;
Sharpe applies √252 to a non-daily series; the history page
recomputes Δ template-side beside a stored diff; the settings table
zero-defaults unavailable metrics. *Proposed:* seed + label the
sample, unify the engines' economics or cross-caption them, label
the sizing model, and render unavailable as "—".

**S17 — Email "Trades Today" selection** ET date prefix matched
against UTC timestamps (boundary mis-bucketing); 200-row cap applied
before the date filter (busy-day truncation); Price column shows
decision price, not fill. *Proposed:* fill-true price, date filter in
SQL on the converted boundary, cap after filtering.

**S18 — Email AI block definitions** Blended win rate (HOLDs in)
differs silently from /ai's directional; profit factor sums
percentages and can print `inf`; averages carry no sample size.
*Proposed:* mirror /ai's directional split + HOLD quality, dollar-
or clearly-labeled percent PF, and n on every average.

**S19 — /admin API-calls plumbing** Per-user dollar cost computed
every render and never displayed; profile DB paths are bare relative
filenames (CWD-fragile — the 2026-07-24 resolver class). *Proposed:*
add the cost column; use the canonical path resolver.

## Resolved

**R1 — Per-trade CVaR/VaR option basis** — missing 100× multiplier,
then decision-price garbage, then per-leg units; fixed 2026-08-12/13
(`bdabbb3`, `5582aac`, `70ffbe2`); fleet-verified floor at −100%.

**R2 — Scratch classifier option basis** — a −$2 option close on $510
basis counted as a −39% loss; fixed with the shared notional
(`bdabbb3`).
