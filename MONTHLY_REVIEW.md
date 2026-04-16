# Monthly Review — QuantOpsAI

A follow-up tracker for the first 3 months of live paper trading with all 10 phases of the Quant Fund Evolution roadmap operational. Fill in the "Observed" columns at each month-end review.

**Start date:** 2026-04-14
**Review date (M1):** ~2026-05-14
**Review date (M2):** ~2026-06-14
**Review date (M3):** ~2026-07-14

---

## 0. Baseline Context

### What an equivalent system would cost in real life

| Component we built | Commercial equivalent | Annual cost |
|---|---|---|
| SEC filings semantic analysis | AlphaSense seat, FactSet SEC module, Bloomberg legal/regulatory feed | $15–40k/seat |
| Options oracle | OptionMetrics, TradeAlert, Bloomberg options analytics | $15–180k/yr |
| Alt-data (insider, short interest, options flow) | FactSet, S&P Capital IQ, Refinitiv | $25–100k/yr |
| Rigorous backtesting platform | Axioma Portfolio, Bloomberg PORT, QuantConnect enterprise | $25–150k/yr |
| Crisis / risk monitoring | MSCI RiskManager, Bloomberg risk suite | $50–200k/yr |
| Multi-strategy capital allocation | Axioma, MSCI BarraOne | $50–200k/yr |
| Bloomberg Terminal (baseline) | — | $24k/seat |

**People a small fund would hire** (PM + 2 analysts + quant dev + risk manager): ~$2–3M/yr in total comp.

**Essentially no commercial equivalent** (every fund builds itself):
- Meta-model on own predictions
- Alpha decay auto-retirement
- Strategy auto-generation with AI-proposed specs
- Specialist AI ensemble with structured verdicts
- End-to-end event-driven architecture

**Engineering to build and operate the non-commercial layers**: 2–3 engineers × $300–500k = $1–1.5M/yr plus 12–18 months initial build.

**Conservative total for a small systematic fund replicating this stack**: $3–6M/yr ongoing, $2–4M initial build.
**Our run cost**: ~$15/mo hosting + $100–500/mo AI inference with all 10 phases active.

### What this system is — and isn't

**What it is:** a decision-support system with automated execution and learning loops, running on paper trading. The strategy library is 16 hand-coded strategies covering mean-reversion, momentum, event-driven, microstructure, and technical confirmation families — plus any AI-generated auto-strategies accumulated via Phase 7.

**What it isn't:** proven. When Phase 2 rigorous validation ran against the first-wave individual strategies, **all 5 new ones failed in isolation** (Sharpe < 1.0, 100% OOS degradation, 0 profitable regimes). Many of the 10 additional seed strategies rely on classical academic anomalies that have decayed in published follow-up studies. The bet is that the *ensemble* — 16 strategies combined with AI judgment, meta-learning, specialist consensus, and crisis gating — produces edge the individual strategies don't have on their own. That's the hypothesis, not a theorem.

**Where edge plausibly comes from:**
1. Breadth of signals the AI sees per decision — things no retail system sees
2. Meta-model learning the AI's error patterns specifically — proprietary data by definition
3. Risk assessor VETOs + crisis gate — asymmetric payoff layers; can only help, never hurt
4. Strategy auto-generation producing variants you didn't think of — compounding layer

**Where it can fail:**
- AI hallucinations and overconfidence
- Slippage and real-fill issues (this is paper trading)
- Regime you haven't trained on (March 2020-style shock)
- Meta-model overfitting on small samples (100 predictions is barely above noise)
- Option-heavy signals degrading in low-volatility environments

---

## 1. Month 1 Review (week 4)

### What should be happening

- [ ] **Meta-model** — not trained yet. Crypto might hit 100 resolved predictions first (runs 24/7). Equities need ~4 weeks of market days.
- [ ] **Auto-strategies** — 4 Sunday proposal cycles × 3 proposals = 12 proposals per profile. Most should fail Phase 2 validation (healthy). Expect **1–3 per profile** to survive into shadow.
- [ ] **Alpha decay** — no deprecations yet (needs 30 consecutive bad days).
- [ ] **Ensemble** — fires every cycle. No measurable signal yet.
- [ ] **Events** — sparse; real triggers are rare.

### Metrics to pull from `/performance#ai`

| Metric | Target range | Observed | Notes |
|---|---|---|---|
| Resolved predictions (crypto) | ≥ 100 | | Meta-model can train |
| Resolved predictions (equity profiles) | 50–150 | | Growing but not trained yet |
| Auto-strategy proposals attempted | 12 per profile | | 4 Sundays × 3 |
| Auto-strategy survival rate (to shadow) | 8–25% | | >25% = gate too lenient; <8% = too strict |
| Strategies deprecated | 0 | | Shouldn't trigger yet |
| Risk VETOs fired | > 0 | | Zero means risk specialist is too permissive |
| Crisis level | normal | | Unless real market event occurred |
| AI inference spend | $100–200 | | Rough guide |

### P&L expectation on paper

**-5% to +5%**. Noise-dominated. Don't over-interpret either direction.

### Red flags

- [ ] Meta-model trained on < 100 samples — if it did, reject and wait
- [ ] Auto-strategy survival rate > 50% — validation gate is broken
- [ ] Zero risk VETOs after 4 weeks — risk specialist not engaging; inspect prompts
- [ ] Same strategy name appearing as both active AND retired — lifecycle bug
- [ ] Event stream empty for 4 weeks — detectors not firing; check scheduler logs
- [ ] Any crisis state transition while VIX < 20 and SPY stable — false positive; recalibrate

### Action items

- Review raw cycle data for 3 random trade decisions; confirm the AI saw the ensemble consensus + SEC alerts + options oracle + crisis context in its prompt
- Check `journalctl -u quantopsai` for any warnings or exceptions in the last 7 days
- Verify at least one event of each type appeared in the last 30 days: `sec_filing_detected`, `earnings_imminent`, `price_shock`, `prediction_big_winner`, `prediction_big_loser`

### Observed (fill in at review)

```
Date of review:
Meta-model status:
Auto-strategy count (proposed / shadow / active / retired):
Alpha decay events:
Paper P&L:
AI spend MTD:
Anything surprising:
Decisions made:
```

---

## 2. Month 2 Review (week 8)

### What should be happening

- [ ] **Meta-model** — crypto profile first-train around week 5–6. Equity profiles around week 7–8. First re-weighting and suppression decisions. **Early models often overfit to noise — initial calibration looks worse before better.**
- [ ] **Auto-strategies** — 2–4 per profile in shadow. None promoted yet (needs 50 resolved predictions + Sharpe ≥ 0.8).
- [ ] **Alpha decay** — possible first deprecation events if any strategy had a rough 30-day run.
- [ ] **Ensemble** — patterns emerge in which specialists tend to be right / wrong. We don't track specialist accuracy explicitly yet (a future enhancement worth considering).

### Metrics to pull

| Metric | Target range | Observed | Notes |
|---|---|---|---|
| Meta-model AUC (crypto) | ≥ 0.52 | | Above random |
| Meta-model suppressed trades | 5–25% of proposals | | >25% = over-suppressing; <5% = not engaging |
| Meta-model training samples | ≥ 100 per profile | | |
| Top 3 important features | sensible (not pure noise) | | E.g., confidence, RSI, sector |
| Shadow auto-strategies | 2–4 per profile | | |
| Shadow Sharpe distribution | mix of +/- | | All negative = proposer is bad; all positive suspicious |
| Deprecated strategies | 0–2 across all profiles | | Above 3 means validation was too lenient upstream |
| Specialist agreement rate | 40–70% | | Too high = redundant; too low = noisy |
| Risk VETOs / total candidates | 2–10% | | Calibration check |
| Paper P&L | -10% to +10% | | Regime-dependent |
| AI spend MTD | $200–500 | | |

### Red flags

- [ ] Meta-model AUC < 0.50 after training — actively worse than random; suspect label leak or feature bug
- [ ] Meta-model suppressed > 50% of proposals — either the AI is terrible or the model overfit
- [ ] Top features dominated by a single noise-looking indicator — likely leakage
- [ ] A shadow strategy's Sharpe suddenly spiking to > 5 — check for look-ahead bias
- [ ] Specialists agreeing on 100% of candidates — they're not actually independent
- [ ] Crisis gate fired during a normal week — thresholds too tight

### Action items

- Review the meta-model's top 10 feature importances on the performance page
- Spot-check 5 suppressed trades — did the AI actually look bad in those cases?
- If any strategy got deprecated, look at its rolling Sharpe curve — was the decay real?
- Review specialist ensemble breakdowns for the 10 worst-performing trades this month — did any specialist flag them?
- Cost trend: verify AI spend is within forecast and not ballooning

### Observed (fill in at review)

```
Date of review:
Meta-model AUC (per profile):
Meta-model suppression rate:
Auto-strategy lineage (gen 1 survivors, gen 2 proposals):
Deprecation events:
Paper P&L:
Biggest single winner / loser:
Specialist with highest "seemed-right" rate:
Anything surprising:
Decisions made:
```

---

## 3. Month 3 Review (week 12) — the first real verdict

### What should be happening

- [ ] **Meta-model** — fully active on all profiles, suppressing low-probability trades. **This is the month that tells you whether meta-model AUC > 0.55.** If AUC ≤ 0.52 the model is not learning anything useful and needs more data or a different feature set.
- [ ] **Auto-strategies** — first shadow → active promotions become possible. Some will retire.
- [ ] **Alpha decay** — 1–2 deprecations likely. This is the system admitting what's dead.
- [ ] **Event-driven** — enough accumulated history to see which event types actually move the portfolio.
- [ ] **Crisis gate** — probably never fired (needs sustained VIX > 22). If it did fire, that's the layer paying for itself.

### The go/no-go metrics

| Metric | Green (continue) | Yellow (investigate) | Red (pause and diagnose) |
|---|---|---|---|
| Meta-model AUC, average across profiles | ≥ 0.55 | 0.52–0.55 | < 0.52 |
| Paper P&L over 3 months | > 0% | -10% to 0% | < -10% |
| Sharpe (approximate, annualized from 3mo) | > 0.5 | 0.0–0.5 | < 0.0 |
| Auto-strategies active | ≥ 1 per profile | 0, but shadows viable | 0 and all shadows failing |
| Specialist VETO accuracy | majority of vetoed symbols underperformed | mixed | VETOs systematically wrong |
| Crisis false-positive rate | 0 firings in normal weeks | rare | any firing during calm market |
| Alpha decay triggers | 0–2 | 3–4 | > 4 (gate was too lenient upstream) |
| AI spend MTD | $300–800 | | > $1500 |

### What 3 months of data *will* tell you

- **Meta-model AUC** — above 0.55 = learning real patterns; below 0.53 = still noise
- **Specialist agreement rate** — whether they're genuinely diverse or redundant
- **Auto-strategy survival rate** — if >20% of proposals clear validation, the proposer is over-approving; if <5%, under-approving
- **Crisis signal false-positive rate** — any firing in calm conditions means thresholds are too tight
- **Alpha decay triggers** — should be rare; if >30% of strategies got deprecated, validation gate was too lenient upstream

### What 3 months will *not* tell you

- Whether the system is profitable long-term. Minimum reliable Sharpe measurement needs 1–2 years
- Whether it survives a true regime break — you need a market shock during the window to test Phase 10
- Whether auto-generated strategies produce real alpha — needs 50+ resolved predictions each, so earliest measurable is month 4–5

### Decision tree at month 3

```
Meta-model AUC ≥ 0.55 AND Paper P&L > 0% → continue another 3 months of paper
Meta-model AUC ≥ 0.55 AND Paper P&L negative → keep learning; AI judgment may
    still be improving; check if drawdown correlates with specific regime
Meta-model AUC 0.50–0.55 → insufficient data; extend 3 months
Meta-model AUC < 0.50 → feature set or label construction is broken; diagnose
    before adding more training data
```

### Observed (fill in at review)

```
Date of review:
Meta-model AUC per profile (crypto, equity1, equity2):
Paper P&L (total %, Sharpe, max drawdown):
Auto-strategies promoted to active:
Auto-strategies retired:
Total deprecations (Phase 3):
Crisis gate firings:
Most material learning this quarter:
Decision for months 4-6:
```

---

## 4. Ongoing Hygiene (monthly)

- [ ] Check `journalctl -u quantopsai --since '30 days ago' | grep -i 'error\|exception\|warning'` for unexpected failures
- [ ] Verify dashboard panels all render (AI Cost, Strategy Allocation, Evolving Library, Specialist Ensemble, Event Stream, Crisis Monitor)
- [ ] **AI Cost panel** — confirm 30-day spend is in the expected band (~$200–700); check that no single `purpose` is dominating unexpectedly. The breakdown by `purpose` and `model` columns are the truth, not invoice screenshots.
- [ ] Pull one random cycle's `cycle_data_*.json` and sanity-check the AI saw everything we think it saw
- [ ] **DB backups** — verify `/var/backups/quantopsai/` on the droplet has a fresh entry per profile dated within the last 24h, and that the last 14 days are present (rotation working). Per-profile DBs hold all proprietary training data — the meta-model is useless without them.

---

## 5. When to Go Live with Real Capital

**Do not** until **all** of these are true:

- [ ] 6+ months of continuous paper trading
- [ ] Positive out-of-sample Sharpe (out-of-sample = after meta-model was trained)
- [ ] Meta-model AUC ≥ 0.58 sustained for 2+ months
- [ ] At least one full regime transition observed (e.g., bull → sideways → bull) with stable behavior across
- [ ] Crisis gate never fired a false positive in calm conditions
- [ ] At least one auto-strategy successfully promoted, lived in active for 30+ days without deprecation
- [ ] Max drawdown on paper < 15%
- [ ] Total cost per trade (AI + infra) < 0.5% of per-trade notional

Everything before this is **learning which assumptions were wrong**, not trading.
