# /learning — Learning Scoreboard — Calculation Register

Source: `learning_scoreboard.py` (`collect_scoreboard`), fed by each
profile's `ai_predictions` and `daily_snapshots`, the master DB's
`trading_profiles` (arm = `ai_provider:ai_model`) and `tuning_history`,
`virtual_benchmarks` snapshots, and SPY daily returns via
`metrics.legacy._fetch_benchmark_returns` (Alpaca bars). View
`views.learning_page`, template `templates/learning.html`. Written
2026-08-23 BEFORE the page (docs/25 step 2.1) so every number is
defined before it is displayed.

**Purpose of the page.** "Is the system learning" must be a slope on
a chart, not a feeling: per arm, per ISO week, out of sample, with
the arm's replicates shown side by side. "Better model" means one
arm's lines sit above the others across all its replicates. The page
issues NO verdict — the pre-registered decision rule (docs/25 §3)
does that at the horizon.

## Definitions

**Arm** — `ai_provider:ai_model` of the profile; replicates = all
enabled profiles with that pair. A profile with `strategy_type`
other than `ai` is not an arm. Prediction rows count toward the arm
only when their `ai_model` is the profile's current model (docs/25
5.4), so a promoted primary's curve is its own from its first week.
**VERIFIED** (fixture: 10 current-model + 10 prior-model rows → n=10).

**Week** — ISO week (`YYYY-Www`) of the prediction's `timestamp`
(decision time, not resolution time) so a week's row is fixed once
its predictions resolve and never moves. Equity and SPY rows use the
ISO week of the snapshot/bar date. **VERIFIED** (fixture)

**Resolved directional set** — `status='resolved'`,
`actual_return_pct IS NOT NULL`, `data_quality` untagged,
`predicted_signal` ∈ {BUY, STRONG_BUY} (bullish) or {SELL, STRONG_SELL,
SHORT} (bearish). **Hit** = bullish and return > 0, or bearish and
return < 0. `hit_rate = hits / n`. *Why raw sign:* the same rule the
shadow grader uses (`actual_return_pct` is the raw price move, verified
2026-07-25). **VERIFIED** (fixture)

**Calibration** — two readings: (a) hit rate at stated confidence
≥ 70 vs < 70, each with n; (b) **Brier score** =
mean((confidence/100 − hit)²) over the directional set — 0 is perfect,
0.25 is what a coin with 50% stated confidence scores; lower is
better and it rewards honest confidence, not high confidence.
**VERIFIED** (fixture: all-correct at 100% stated → 0.0; all-correct
at 50% stated → 0.25).

**HOLD quality** — resolved HOLD predictions where |return| < 1.5%
(the same ±1.5% band the shadow grader uses for a correct neutral
call) over all resolved HOLDs. **VERIFIED (code)**

**Mean move** — mean `actual_return_pct` over the directional set
(signed by the raw price move, NOT by position direction — a bearish
hit has a negative move). Shown for context, never as a P&L.
**VERIFIED (code)**

**Weekly equity return (per replicate)** — `last snapshot equity in
week / last snapshot equity in prior week − 1`; the first week with no
prior snapshot is absent. **Arm equity return** = mean across the
arm's replicates that have a value that week, with the replicate
min/max shown. **VERIFIED** (fixture)

**SPY weekly return** — product of (1 + daily return) over the week's
trading days from Alpaca bars, minus 1. **Excess return** = arm weekly
equity return − SPY weekly return. Absent when SPY bars are
unavailable — never 0. **VERIFIED** (fixture)

**Benchmark band** — for the same week: Buy-Hold-SPY virtual
benchmark's return, and the Random replicas' min / median / max weekly
returns (the null band an arm must clear). **VERIFIED (code)**

**Tuner scorecard (per arm)** — counts of `tuning_history.outcome_after`
(improved / worsened / unchanged / pending / other) for the arm's
profiles; the share `improved / (improved + worsened)` with n. Shown so
the tuner's own assessment sits next to the outcome curves it claims
to move. **VERIFIED (code)**

## Conventions honored
- Any rate from an empty sample renders "—" (README convention 5).
- Weeks with fewer than 10 directional resolutions are flagged
  `(thin)` in the table; the number is still shown with its n.
- All-time scope: nothing rolls off.

## Open items for this page

| ID | Item | Concern |
|---|---|---|
| — | Brier on stated confidence | the meta-model adjusts stated confidence before the gate on meta-enabled profiles; this page scores the STATED number (what the model said), which is the calibration question — noted so an auditor doesn't expect the blended value |
