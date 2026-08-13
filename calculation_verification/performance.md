# /performance — Calculation Register

Source of computation unless noted: `metrics/legacy.py`
(`calculate_all_metrics`), fed by `_gather_trades` (all closed rows
with non-NULL pnl, `data_quality IS NULL`) and `daily_snapshots`
(one equity row per day, deduplicated to `MAX(rowid)` per date).
View wiring: `views.py` route `/performance`.

Verification pass: 2026-08-13, against code at commit `70ffbe2` and
live profile data (p210–p219). Conventions (fill-true pricing, the
option 100× multiplier, dead-status exclusion) are defined in
[README.md](README.md) and apply throughout.

---

## Headline strip

**Total Return** — `(last_equity − first_equity) / first_equity × 100`
over the profile's `daily_snapshots`. *Why:* equity-curve based rather
than trade-sum based, so unrealized P&L and cash drag are included;
"as of" date is stamped beside it because the trades page's realized
figure legitimately differs intraday (2026-07-15 operator confusion,
p216). **VERIFIED**

**Days Active** — calendar days between first and last trade
timestamps, floor 1. **SUSPECT — S1:** the numerator of Total Return
spans the *snapshot* range while Days Active spans the *trade* range.
A profile that idled before its first trade or after its last one
annualizes on a mismatched window. See SUSPECTS.md S1.

**Annualized Return** — `((1+total_return)^(365/days_active) − 1) ×
100`, only when `days_active ≥ 7` and total_return > −100% (else 0
with `…_computable=False`). *Why:* the guard exists because one hot
day compounds to nonsense ((1.05)^365 overflows); 7 days is the
minimum span where annualizing says anything. **VERIFIED** (subject to
S1's window mismatch).

**Total P&L** — `Σ pnl` over all closed rows. Realized only; the
live-reconciliation strip beside it carries the live split. **VERIFIED**

**Live reconciliation strip (total / realized / unrealized)** — from
`journal.get_virtual_account_info` + `get_virtual_positions` at
request time: realized = Σ closed pnl, unrealized = Σ open-position
`(current − entry) × qty × mult`, total = sum. *Why:* proves the
page's historical numbers tie to the live book in one glance.
Degraded books render "unavailable" (README convention 4).
**VERIFIED**

## Ratios

**Sharpe Ratio** — `mean(daily_returns) / std(daily_returns) × √252`,
requiring ≥ `MIN_RETURNS_FOR_SHARPE` daily returns; 0 with
`computable=False` below that. Daily returns are snapshot-to-snapshot
equity ratios. *Why √252:* daily→annual scaling on trading days.
**SUSPECT — S2 (two parts):** (a) no risk-free-rate subtraction — at
~5% short-term rates this overstates Sharpe by roughly `rf/vol`; fine
for comparing profiles against each other, misleading against
industry benchmarks. (b) the daily-return series has a gap-fill
branch: when consecutive snapshots are missing, it falls back to
`daily_pnl / FIRST_equity` — dividing today's pnl by the account's
*original* equity, which under/overstates the return once equity has
drifted from its start. See SUSPECTS.md S2.

**Sortino Ratio** — `mean(daily_returns) / std(negative_returns_only)
× √252`. **SUSPECT — S3:** textbook Sortino divides by *downside
deviation* — `sqrt(Σ min(r,0)² / N)` over ALL N days — not the
standard deviation of only the negative days. The current variant
overstates the denominator when losses are rare-but-similar and
understates it when they're rare-but-varied; either way it is not the
figure an auditor will recompute. See SUSPECTS.md S3.

**Calmar Ratio** — `annualized_return / max_drawdown`, only when
DD ≥ 1% AND ≥ 30 days of history (else 0, not-computable). *Why the
guards:* 1 day of data with a 0.07% DD produced −310 (recorded in
code comment); both floors must hold before the ratio is honest.
**VERIFIED**

## Risk section (#risk)

**Annualized Volatility** — `std(daily_returns) × √252 × 100`. Same
daily-return series as Sharpe, so S2(b)'s gap-fill caveat applies.
**VERIFIED (code)**, inherits S2(b).

**Per-Trade VaR (95%)** — 5th percentile (linear interpolation; numpy
`percentile` or an exactly-matching pure-Python fallback) of
per-DECISION return percentages, minimum 5 decisions. **VERIFIED**

**Per-Trade CVaR (95%)** — mean of all decision returns ≤ the VaR
threshold. *Why per-decision:* per-LEG returns are meaningless for
tails — a defined-risk spread's legs legitimately score ±600%
individually while the decision was a small bounded bet. Option legs
group into their spread episode (profile, underlying, strategy,
OCC-embedded expiry, close date) and score `Σpnl / capital-at-risk`
(`spread_max_loss` sum when carried, else premium notional); stock
trades and single-leg options are their own episodes. History: this
register exists partly because this cell displayed −4360.5% on
2026-08-12 (missing option multiplier), and −8409% in May
(corrupted-row contamination). Fleet-verified 2026-08-13: worst
decision return anywhere is −100.0% — the mathematical floor.
**VERIFIED**

**Max Drawdown** — peak-to-trough on the snapshot equity curve:
running peak, `dd = (peak − eq)/peak`, maximum retained with peak and
trough dates. **VERIFIED**

**Max Drawdown Duration** — days from the drawdown's peak date to the
first snapshot whose equity re-crosses the peak; if never recovered,
days from peak to the LAST snapshot (i.e., still counting). *Why:* an
unrecovered drawdown that stopped counting would understate exactly
when it matters. This is why the cell can read "32 days" while the
dd window shows only 07-10→07-14: the trough was early but the peak
was never re-crossed. **VERIFIED**

## Returns detail

**Gross vs Net Return** — net = realized pnl (post-slippage,
fill-true); gross = net + signed slippage cost from
`journal.get_slippage_stats` (side-aware sign, spans buy AND sell
fills). *Why the canonical aggregator:* the pre-2026-05-16 version
summed `|slippage| × price × qty` per closed row, double-counting
favorable slippage and missing the buy side entirely. **VERIFIED**

**Monthly Returns table** (month, trades, wins, losses, scratch, pnl,
return %) — trade counts from closed rows bucketed by close month;
monthly return compounds the snapshot daily returns inside the month.
**VERIFIED (code)** — but the win/loss/scratch columns count LEGS,
see S4.

**Monthly Win Rate** — % of months with positive monthly pnl.
**VERIFIED**

**Rolling 3-month return / 6-month Sharpe** — 63- and 126-trading-day
windows over dated snapshot returns, stepped ~monthly (21 days);
suppressed with an explanatory note until enough days exist.
**VERIFIED**

**Worst week / month / quarter** — minimum compounded return over
rolling 5/21/63-day windows, labeled with the period and the actual
day count when the window is partial. **VERIFIED (code)**

## Trade statistics

**Win Rate / Winning / Losing / Scratch counts** — per closed ROW:
scratch when `|pnl/notional| < 0.5%` (or pnl exactly 0 / no basis);
win/loss otherwise by pnl sign; win rate = wins / (wins + losses),
scratches excluded from the denominator. Notional is fill-true with
the option multiplier. *Why exclude scratches:* a break-even exit
says nothing about edge; counting it as a loss deflates win rate for
tight-stop styles. **SUSPECT — S4:** counts are per-LEG while the
return *distributions* (VaR/CVaR, avg win/loss) are per-EPISODE. A
closed spread contributes up to 2 wins+losses to the counts but one
figure to the distributions. Deliberate (counts answer "how many
rows resolved how"), but the unit inconsistency needs an explicit
operator decision. See SUSPECTS.md S4.

**Profit Factor** — `Σ wins / |Σ losses|` in dollars; undefined
(rendered "—") when either side is empty. **VERIFIED**

**Expectancy** — mean pnl per closed row, dollars. **VERIFIED**

**Avg Win % / Avg Loss %** — mean of per-EPISODE positive /negative
return percentages (fill-true, multiplier-correct). **VERIFIED**

**Win/Loss Ratio** — `avg_win_pct / |avg_loss_pct|`; "—" when either
side missing (never 0, README convention 5). **VERIFIED**

**Largest Win / Loss** — max/min single-row pnl with symbol.
Per-leg by construction; a spread's monster leg can appear here even
when the spread netted small — acceptable for a "records" cell but
noted under S4. **VERIFIED (code)**

**Avg Hold Days / Trades per Day / per Month** — hold = close minus
open timestamps matched FIFO per symbol; frequency = closed-row count
over active span. **VERIFIED (code)**

**Streaks (current / max consecutive wins / losses)** — over closed
rows in close order, scratches break neither streak. **VERIFIED (code)**

## Benchmarks & factors

**Alpha / Beta (SPY) / Correlations (SPY, QQQ, BTC)** — daily profile
returns regressed against benchmark daily returns fetched via the
market-data layer (cached); beta = cov/var, alpha = intercept
annualized, correlations are Pearson r. Suppressed below the minimum
overlap. **VERIFIED (code)** — spot-check of the benchmark return
series against an independent source is queued as S5 (the fetch path
is Alpaca bars; a stale/partial series would silently skew beta).

**Exposure (gross %, positions, bands, factor tilts)** — from
`pipelines/risk/exposure.py`: stock exposure `|qty × price|`, options
via `delta × spot × 100 × qty` (delta-adjusted, deliberately not
premium), bands and factor buckets share those notionals. *Why
delta-adjusted:* premium massively understates the directional
exposure an option carries. **VERIFIED**

**Kelly (long / short)** — `f* = (bp − q)/b × 0.25` (quarter-Kelly),
`b = avg_win/avg_loss`, `p = win_rate` over the direction's closed
rows; returns "no edge" (None) on negative Kelly and refuses
fractional recommendations above 50% of capital as untrustworthy
inputs. *Why quarter-Kelly:* full Kelly's variance is intolerable in
practice; 0.25 is the house risk posture. **VERIFIED** — inputs are
per-leg stats, so S4's unit caveat touches this too.

## Charts

**Equity curve / drawdown / monthly bars / pnl distribution SVGs** —
direct renderings of the series documented above; no independent
math beyond axis scaling. **VERIFIED (code)**

---

## Open items for this page

| ID | Item | Concern |
|---|---|---|
| S1 | Annualized return window | trade-span days vs snapshot-span return |
| S2 | Sharpe | no risk-free rate; gap-fill divides by first-day equity |
| S3 | Sortino | std-of-negatives instead of downside deviation |
| S4 | Win/loss counts vs distributions | per-leg counts, per-episode distributions |
| S5 | Benchmark series | needs independent spot-check of fetched bars |

All five are mirrored in [SUSPECTS.md](SUSPECTS.md) with proposed
resolutions.
