# /ai (Brain · Strategy · Awareness · Operations) — Calculation Register

One page, four tabs (`/ai/brain` etc. are redirects to anchors).
Computation sources: `views.py ai_dashboard` (~4189–4814) +
`_ai_common`, `_build_long_short_awareness`,
`_build_portfolio_risk_awareness`; `ai_tracker.py`, `journal.py`,
`meta_model.py`, `online_meta_model.py`, `kelly_sizing.py`,
`alpha_decay.py`, `multi_strategy.py`, `risk_parity.py`,
`portfolio_exposure.py`, `crisis_state.py`, `event_bus.py`,
`options_greeks_aggregator.py`, `cost_guard.py`, `ai_cost_ledger.py`,
`signal_weights.py`, `self_tuning.py`, plus the live APIs
(slippage model/history, Monte Carlo, macro, attention, tuning,
autonomy, resolver).

Verification pass: 2026-08-13 at `70ffbe2`.

---

## Brain tab

**Prediction counts (Total / Resolved / Pending)** — straight
`COUNT(*)` splits on `ai_predictions.status`, summed across profile
DBs. **VERIFIED**

**AI Trade Win Rate** — directional predictions only
(BUY/SHORT/SELL/STRONG_SELL/MULTILEG_OPEN) with win-or-loss outcomes:
`wins / resolved × 100`. *Why directional-only:* a HOLD can't "win" a
trade; mixing it in flattered the number (split shipped earlier this
year). **VERIFIED**

**HOLD Pass Quality** — % of resolved HOLDs that were right to sit
out. Cross-profile aggregation reconstructs win counts from each DB's
percentage (`round(n × pct/100)`), guarded by a range assertion.
**SUSPECT — S11:** round-tripping a percentage back into a count is
lossy; the tracker should return the raw win count so aggregation
sums integers. Off-by-≤1 per profile, but an auditor recomputing will
hit mismatches.

**"Prediction Accuracy" tile** — displays a PROFIT FACTOR:
`Σ positive resolved returns / |Σ negative|` over non-HOLD resolved
predictions. **SUSPECT — S9:** the label says accuracy, the number is
a profit factor — an auditor reading label-first will call this
wrong, and they'd be right. Rename the label (or show accuracy).

**Win-rate trend chart** — 7-day rolling `wins/(wins+losses)` per day
over 60 days, ET-anchored; line breaks (None) on empty windows rather
than faking 0% (README conv. 5). **VERIFIED**

**Confidence calibration (avg conf on wins / losses)** — unweighted
mean of `confidence` over resolved rows per outcome, across all
profiles (row-weighted, not profile-weighted — larger books count
more, documented as intended). **VERIFIED**

**Move by prediction type (long / short / exit-long)** — mean
`actual_return_pct` per `prediction_type`, rendered only at n ≥ 10.
**VERIFIED**

**Best/Worst trade, biggest missed gain / avoided loss** —
per-DB extreme rows (`trade_pnl_pct` sign-adjusted for shorts; HOLD
extremes by raw return), cross-profile max/min. **VERIFIED (code)**

**Slippage impact (avg % / net cost / variance / n)** — stock-only,
data-quality-excluded; avg is trade-count-weighted across profiles;
net cost is the side-aware signed sum; variance is the unsigned
magnitude. *Why signed AND unsigned:* net answers "what did it cost",
magnitude answers "how noisy is execution" — favorable slippage nets
against cost but still counts as variance. **VERIFIED**

**Meta-model panel (AUC / accuracy / samples / base rate / top
features)** — from the trained bundle: AUC on the last-20%
time-ordered holdout (never a random split — leakage), gradient-
boosting feature importances top-10; online-layer counters from the
SGD bundle. **VERIFIED (code)**

**Slippage model / drift / Monte Carlo / options-backtest panels** —
documented per formula in the extraction record: calibrated K in bps
per √participation with bootstrap residuals; drift = realized−
predicted bps with mean/σ/Pearson r (n-guards 2 and 5); MC replays
closed round-trips (≥5) with clamped sim counts; options backtest
summarizes win rate / totals / Sharpe-proxy `avg/σ(n−1)`.
**VERIFIED (code)**

## Prompt-side numbers — the track-record block (in-context learning)

Source: `calibration_block.py` (`compute_track_record`,
`render_track_record`), injected by `trade_pipeline` into the batch
context as `calibration_block` and rendered by
`ai_analyst._build_batch_prompt` after the learned-patterns section.
Added 2026-08-23 (docs/25 step 4.2). Every shadow arm receives the
same prompt, so the block is identical across arms.

**Resolved set** — `status='resolved' AND actual_outcome IN
('win','loss')`, `data_quality`-tagged rows excluded; scratch outcomes
are outside the denominator — the same definition as the /ai Brain
tab's directional win rate (S12's chosen definition). **VERIFIED**
(fixture: 15W/5L with 10 scratch + 10 tagged + 10 pending rows → 20
resolved, 15 wins).

**Per bucket** — `win rate = wins / n`, `mean move = AVG(actual_return_pct)`
over the bucket; buckets: all-time, last 30 days (by `resolved_at`),
stated-confidence band (0–25 / 25–50 / 50–75 / 75–100), call family
(BUY incl. STRONG_BUY; SELL incl. STRONG_SELL; SHORT; HOLD), top-6
`strategy_type`, top-4 `regime_at_prediction`. Any bucket with n < 10
renders as `n<10 (not enough to judge)` — never a percentage; a
profile with < 20 resolved rows renders a one-line statement, never
0%. *Why:* a model shown "100% on 2" will over-weight it; the n is on
every number so the model (and an auditor) can discount thin buckets.
**VERIFIED** (fixture: 25-sample band shows 100%, 3-sample band shows
n<10; recent window splits by resolved_at).

**Failure behaviour** — an unreadable profile DB logs a WARNING and
renders nothing; the prompt proceeds without the block. **VERIFIED**

## Strategy tab

**Validation scores** — stored `passed_gates/total_gates × 100` from
the rigorous backtester's gate run. **VERIFIED (code)**

**Strategy allocation weights** — Sharpe-derived: raw = clamped
rolling Sharpe (≤4), cold-start default `1/N` under 20 lifetime
predictions, ×0.25 on non-positive Sharpe, normalized then
iteratively capped at 40% with redistribution. *Why the cap:* one hot
strategy must not become the book (the 40% cap + redistribute is the
same shape as the tested capital-allocation invariants). **VERIFIED**

**Rolling/lifetime Sharpe per strategy** — mean/σ×√252 over that
strategy's resolved prediction returns (30-day window vs all-time);
Edge-change % rendered only when lifetime Sharpe ≠ 0 AND lifetime
n ≥ 50. **VERIFIED** — note these are prediction-return Sharpes, not
equity-curve Sharpes; the page prose says so.

**Strategy library counts / generation tags** — status counters over
`auto_generated_strategies`. **VERIFIED (code)**

## Awareness tab

**Long/short construction** — short share = Σ sector short% over
gross; balance state from the mandate gate; book beta = Σ weighted
per-name betas with short signs fliped before weighting; Kelly
long/short = quarter-Kelly over ≥30 resolved directional predictions
(else "insufficient data" — never a number). **VERIFIED**

**Drawdown scale** — linear interpolation over the drawdown schedule,
floored at 0.25×. **VERIFIED (code)**

**Risk budget** — per-name `weight × realized vol` contributions vs
the book average; ≥2× flagged over-contributing, ≤0.5× under; refuses
below 2 names with known vol. **VERIFIED (code)**

**Barra-style risk panel** — latest `portfolio_risk_snapshots` row:
daily σ, parametric VaR/ES dollars, factor βs (top 6 by |β|), risk
decomposition shares normalized by Σ|component|, stress scenarios.
**SUSPECT — S13:** the tile labeled "99% VaR (Monte Carlo)" renders
the `mc_var_95_dollars` column — label and stored quantile disagree
(a genuine 99% column also exists, unused). One of the two is wrong.

**Sector concentration** — sectors at gross ≥30%. **VERIFIED (code)**

**Crisis monitor / events / macro / attention** — pass-throughs of
stored state (crisis level + size multiplier, event severities,
FRED/skew/yield readings, z-scores) with formatting only.
**VERIFIED (code)**

**Veto activity** — 7-day verdict counts per specialist; "effective"
vetoes count only VETO-authorized specialists, and the claimed-vs-
effective split is shown precisely so advisory specialists' VETO
votes aren't mistaken for blocks. **VERIFIED**

**Confidence Floor table** — 30-day CONFIDENCE_GATE drops joined to
their counterfactually-scored predictions via `trade_drops.pred_id`;
banded on the GATED (post-meta-blend) confidence in 5-wide bands;
would-be wins/losses and resolved-weighted avg would-be return;
"pending" (never 0) below resolution. *Why:* this is the operator's
evidence base for raising the floor — by design the tuner cannot.
**VERIFIED**

**Book Greeks** — Σ per-leg Δ/Γ/vega/θ from the aggregator with
per-leg detail; amber/red at 80%/100% of the profile's Greek budget
caps; fallback-IV and expired-skip counts disclosed. **VERIFIED**

**Stat-arb pair book** — stored pair stats (hedge ratio, p-value,
half-life, correlation), active only, capped 20. **VERIFIED (code)**

**Ensemble cycle rows** — consensus % = winning side's share of
weighted specialist scores (calibrated confidences × specialist
weights, floor 25 ignored; veto ⇒ 100); chips show calibrated
per-specialist confidence. **VERIFIED**

## Operations tab

**Tuning status pills** — resolved/20 threshold, upward optimization
noted at ≥30. **VERIFIED (code)**
**Tuning history** — categorized 7-day counts
(tighten/refine/loosen/neutral), formatted old→new values, stored
win-rate-at-change and outcome. **VERIFIED (code)**
**Cost guard** — same today/ceiling/headroom/7d-avg chain as the
dashboard banner (single source, `cost_guard.status`). **VERIFIED**
**Signal weights matrix** — override counts and per-cell weights;
"overridden" = present in the non-default store; ≤0 renders DISABLED.
**VERIFIED (code)**
**Autonomy state/timeline, parameter resolver** — pass-throughs of
override stores; resolver shows the global→TOD→regime→symbol chain
with most-specific-wins and the capital scale at execution.
**VERIFIED (code)**
**AI cost panel** — today (ET-midnight-anchored) / 7d / 30d sums,
by-purpose and by-model 30d groupings, straight ledger sums.
**VERIFIED**
**"What the AI Sees" counts (15 indicators / 34 sources / …)** —
hard-coded prose numbers, not computed. **SUSPECT — S14:** static
counts drift as sources are added/removed; either compute them or
mark the text as illustrative.

---

## Open items for this page

| ID | Item | Concern |
|---|---|---|
| S9 | "Prediction Accuracy" tile | label says accuracy, value is profit factor |
| S11 | HOLD pass-rate aggregation | reconstructs counts from rounded percentages |
| S13 | "99% VaR (Monte Carlo)" tile | renders the 95% MC column |
| S14 | "What the AI Sees" counts | hard-coded, will drift |
