# 26 — Experiments Register

One entry per experiment the platform has run or is running: what it
set out to learn, what was actually done, what it taught us, why it
ended, and **exactly where in git and in the data to return to** if we
ever want that moment back. The active plan for the current experiment
lives in [25 — Model Selection & Learning Plan](25_MODEL_SELECTION_AND_LEARNING_PLAN.md);
this register is the history and the rationale.

---

## Experiment 1 — System Stability (2026-05-17 → 2026-08-23) — RETIRED

### What it was designed to answer
The v2.1 design ([15 — Experiment Design](15_EXPERIMENT_DESIGN_2026_05_17.md))
put 13 virtual profiles across three Alpaca paper accounts to answer
five questions: does the system beat null benchmarks; which components
(alt-data, meta-model, self-tuning, options) pull weight; are they
complementary or redundant; is a $25K real-money deployment ready; does
the strategy scale. Baselines were Buy-Hold-SPY and two Random-pick
replicas; ablation arms each switched one component off; two
capital-scale arms ($25K pair, $250K conservative, $700K aggressive).

### What was actually done
The experiment was restarted **thirteen times** (05-18 outage, 06-04
orphan class, 06-09/06-12/06-17 funding and key incidents, 06-19
phantom-equity reset, 06-24, 06-30, 07-08 after the broker-side wipe
of 07-07). Each restart was forced by a class of book-integrity defect,
and each defect class was eliminated at the root rather than patched:

- book-integrity gate halting entries on any broker/journal drift (06-18)
- the oversell door — a profile sells only its own journaled long (06)
- the per-cycle freshness invariant: no order without same-cycle
  reconciliation to the profile's own broker truth (06-23, `619fd9b`)
- one sell-side reservation per slice; broker-backing guard for
  protectives; cancel-on-close for orphaned protectives (06-23)
- canceled ≠ unfilled: fills are read before any journal mutation
  (08-03, after the GOOGL kill-switch incident)
- multileg leg↔order crossing fixed by object identity (08)
- the evidence hierarchy + resurrection net for wrongly voided rows
  (08-11, NEE/BAC)
- fill-true pricing and the ×100 option basis across every metric
  (08-12/13, the −4360% CVaR)
- lint gate as part of the test suite; the Calculation Verification
  Register (every displayed number auditable, 08-13)
- shadow evaluation with exact decision-id joins, funnels, standings,
  all-time scope (08-06 → 08-21)

By 2026-08-21 the fleet reconciled to the broker within ±$3 per profile
and the drift classes that had forced every restart had not recurred
since the freshness invariant shipped.

### Final scoreboard (cohort of 2026-07-08, read 2026-08-21)

| Profile | Equity 07-08 → 08-21 | Directional hit rate |
|---|---|---|
| BuyHoldSPY (baseline) | +2.6% | — |
| RandomA / RandomB (baselines) | −4.6% / +4.8% | — |
| FullSystem (anchor) | **+6.6%** | 46.9% (n=1,159) |
| NoAltData | +6.2% | 50.0% |
| NoMetaModel | −2.7% | 40.0% |
| NoSelfTuning | −2.1% | 53.9% |
| NoOptions | +1.9% | 59.8% |
| NoAltData-NoMetaModel | −6.4% | 46.5% |
| 25K-Candidate / 25K-Replica | −6.5% / −0.5% | 37.6% / 39.6% |
| 250K-Conservative | +1.2% | 46.0% |
| 700K-Aggressive | +1.1% | 61.2% |

SPY over the same window: +3.7%. With one profile per arm, none of
these differences is statistically separable from the Random band
(−4.6% to +4.8%).

### What it taught us — the learnings that define Experiment 2

1. **The platform is now stable.** Execution, reconciliation, and
   measurement hold up under real broker behavior, including a broker
   wiping the accounts. This is the asset the experiment produced.
2. **The system was not learning or improving on its own — it was
   just making decisions.** No model's weights ever change. The
   self-tuner made 429 parameter changes with no measurable improvement
   (win rate 37.0% → 37.9% around changes), mostly on knobs that don't
   bind (`max_total_positions` oscillating 999→749→562→976 while
   `max_position_pct` is the real cap). The "learned patterns" prompt
   mechanism never wrote a single row. The meta-model's high-score half
   beat its low-score half in one week of three. Confidence ≥70 beat
   <70 in two weeks of six. The fine-tune path designed in May
   ([20](20_FINETUNE_PHASE_4B1_INCREMENTAL.md)) was never activated.
   The Settings toggle for model auto-tuning is stored and never read.
3. **The model comparison could not decide anything.** Shadow
   evaluation graded specialist verdicts only — the apex
   trade-selection call returns a set the grader cannot score — and
   every arm was a small-tier model (gemini-3.1-flash-lite, gpt-4.1-nano,
   claude-haiku-4.5). Only one result survived scrutiny: nano beat the
   primary on specialist verdicts (p=0.001; direct vs haiku p=0.0005),
   concentrated where the primary was bearish during an up-month.
4. **One profile per arm can never reach significance.** The ablation
   design asked eight questions with n=1 each; the Random band alone
   spans 9 points. Replicates, not more arms, are what decide.
5. **Cost was concentrated in a losing arm.** Haiku shadowing was $134
   of a ~$175/month bill and measured worse than the primary.
6. **Static benchmarks do not need a broker.** Buy-Hold and Random are
   fire-once portfolios; running them through Alpaca bought nothing but
   exposure to the 07-07 wipe, reset-script complexity, and $750K of
   paper-account capacity.

### Why it is retired
Its five questions cannot be answered by its design (n=1 per arm), its
learning loops were inert, and its model arms never tested whether
intelligence buys returns. Continuing it would have produced more
decisions and no more knowledge. Everything it built carries forward.

### How to return to this exact moment
- **Git:** tag `exp1-system-stability-final` (annotated; the commit
  that adds this document — `git show exp1-system-stability-final`).
  `git checkout exp1-system-stability-final` reproduces the code,
  prompts, metric definitions, register, and reset scripts as they were.
- **Design:** [15](15_EXPERIMENT_DESIGN_2026_05_17.md) (v2.1 arms and
  win conditions); profiles 207–219 on Alpaca accounts 55/56/57;
  manifest in `create_experiment_profiles.py` at that tag.
- **Data:** master DB daily snapshot `/opt/quantopsai/backups/
  quantopsai.db.YYYYMMDD-0500`; per-profile DBs
  `/opt/quantopsai/quantopsai_profile_{207..219}.db`; the cohort's
  learning data is archived to `predictions_archive/` by the reset
  procedure (`RESET_RUNBOOK.md`; `archive_predictions` is a manual
  step — run it BEFORE the wipe).
- **Metric definitions at the time:** `calculation_verification/` at
  the tag.
- **Reset tooling used:** `full_fresh_start_2026_07_08.py` (clone of
  the canonical PM revision), `certify_books.py`.

---

## Experiment 2 — Learning & Model Selection, Phase 1 — RUNNING (start: 2026-08-24)

### What it is designed to answer
1. **Does anything beat the incumbent on real trades?** `gpt-4.1-nano`
   is Experiment 1's measured winner (specialist verdicts, p=0.001;
   head-to-head vs haiku p=0.0005) — it runs as a full arm, and a new
   model is only "better" if it beats nano.
2. Does nano's successor generation (`gpt-5.6-luna`) keep the edge?
   Does a current-generation Google small model (`gemini-3.5-flash-lite`)
   match it at equal tier?
3. **Does stepping up a tier buy returns?** (`gemini-3.7-flash` vs
   `gemini-3.5-flash-lite`, same vendor, so the comparison isolates tier)
4. **Can the system be made to learn** — can its decision quality be
   shown to improve from its own outcomes, on a chart, over weeks?

### Design (details and checklists in [25](25_MODEL_SELECTION_AND_LEARNING_PLAN.md))
- **Four arms × three replicates**, identical capital and settings;
  only `ai_provider`/`ai_model` differ. Arms (Decision D1, 2026-08-23):
  `gpt-4.1-nano` (incumbent) · `gpt-5.6-luna` · `gemini-3.5-flash-lite`
  · `gemini-3.7-flash`. Replicate i of every arm lives on paper
  account i ($250K × 4 = the whole account), so a broker-account event
  hits all arms equally.
- **Shadowing of specialist purposes only**, cross-wise (D5): each
  arm-profile shadows the other three arms on specialist calls; trade
  selection is compared through the replicates' real trades.
- **Baselines move off the broker** (D6, proposed): Buy-Hold-SPY and
  Random are static portfolios, so they are tracked virtually —
  selected once with a recorded seed, marked to market daily from
  Alpaca price data, dividends credited from corporate-action data.
  This frees the reset procedure of the benchmark purchase steps,
  frees paper-account capacity for real arms, removes the baselines
  from broker-wipe exposure, and — because virtual replicas are free —
  lets the Random null run as **ten replicas**, a real variance band
  instead of two draws.
- **Budget:** ≈ $68/month all-in, versus ~$175/month for Experiment 1
  (D0: must stay under Experiment 1's cost).
- **Learning, built and measured, not assumed:** the tuner cut to
  evidence-backed levers; the in-context track record in every prompt;
  the meta-model kept only while it discriminates; the fine-tune path
  activated as a fourth arm when its corpus exists. All of it judged by
  a Learning Scoreboard (per arm, per week, out of sample: calibration,
  hit rate, excess return vs SPY).
- **Pre-registered decision rule:** horizon 8–12 weeks; primary metric
  mean weekly excess return vs SPY across replicates, bootstrap p<0.05;
  an arm whose replicates disagree in sign is undecided; no arm is
  promoted from noise.
- **Vendor fairness:** every provider gets its native structured-output
  path before the first cycle, so the experiment compares models, not
  parsers.

### Why this design
Each element answers a specific Experiment 1 failure: replicates for
n=1; a tier arm for "all small models"; specialist-only shadowing plus
live replicates for the ungradable apex call; the Scoreboard for
"learning was a feeling"; the tuner cut for knob churn; virtual
baselines for broker exposure and capacity; the budget ceiling for a
bill dominated by a losing arm.

### Return points
- Reset applied 2026-08-23 (re-run the same evening with the four-arm
  manifest after the operator's incumbent ruling; no trades had
  occurred); first trading session 2026-08-24. Twelve profiles on
  Alpaca paper accounts `08-24-acct-1..3`, $250K each, one replicate of
  every arm per account (decision D2). Experiment 1's learning data:
  `predictions_archive/<pid>/` for pids 207–219 (170,536 rows).
- Start tag: `exp2-learning-phase1-start` (the commit that recorded
  the reset).
- Decision and progress logs: [25](25_MODEL_SELECTION_AND_LEARNING_PLAN.md) §4–5.
