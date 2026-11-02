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
  learning data was archived by the reset (`RESET_RUNBOOK.md`). Since
  2026-08-23 the reset script archives by itself (step 1c, aborts on
  failure) into `backups/predictions_archive/` — an rsync-excluded
  tree, after a `--delete` deploy erased the original repo-root
  archive that same evening — including the `ai_shadow_calls` rows;
  `archive_predictions()` also maintains `strategy_index.json`, the
  merge-only sidecar the zombie-strategy guardrail reads.
- **Metric definitions at the time:** `calculation_verification/` at
  the tag.
- **Reset tooling used:** `full_fresh_start_2026_07_08.py` (clone of
  the canonical PM revision), `certify_books.py`.

---

## Experiment 2 — Learning & Model Selection, Phase 1 — RETIRED (2026-08-24 → 2026-11-02)

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
- **Baselines move off the broker** (D6): Buy-Hold-SPY and
  Random are static portfolios, so they are tracked virtually —
  symbols selected once with a recorded seed, shares and entry set at
  the first session's OPEN (the same moment the arms can first trade),
  marked to market daily from Alpaca price data, dividends credited
  from corporate-action data, shown on the dashboard as Reference
  Benchmarks.
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

### Measurement-validity notes (read before quoting any Experiment 2 number)
- **The Gemini arms were blind for a fifth of the experiment, and every
  arm's HOLD numbers before 2026-09-20 include decisions no model
  made.** When an arm's PRIMARY decision call failed (provider 429 /
  5xx), the cycle made no decision — but the pipeline journaled a HOLD
  for every candidate, and those rows resolved and were graded like
  real ones. Cycles with no decision, from the 2026-09-20 journal
  snapshot (lost / total):

  | Arm | W35 | W36 | W37 | W38 | All |
  |---|---|---|---|---|---|
  | `gpt-4.1-nano` | 0 / 655 | 93 / 578 (16.1%) | 0 / 472 | 0 / 487 | 93 / 2,192 (4.2%) |
  | `gpt-5.6-luna` | 0 / 710 | 93 / 584 (15.9%) | 0 / 491 | 0 / 537 | 93 / 2,322 (4.0%) |
  | `gemini-3.5-flash-lite` | 106 / 711 (14.9%) | 0 / 588 | **305 / 494 (61.7%)** | 84 / 537 (15.6%) | 495 / 2,330 (21.2%) |
  | `gemini-3.7-flash` | 106 / 705 (15.0%) | 0 / 588 | **304 / 493 (61.7%)** | 84 / 537 (15.6%) | 494 / 2,323 (21.3%) |

  The Gemini failures are `429 RESOURCE_EXHAUSTED`, some reading
  "exceeded its monthly spending cap" — the same account-side cause as
  the shadow-quota item below. Consequences: **8,187 fabricated HOLD
  predictions** (10–16% of all resolved HOLDs on the OpenAI arms,
  20–32% on the Gemini arms), so any HOLD-quality or HOLD-count figure
  read before the fix is contaminated, unevenly across arms; and in
  W37 the Gemini arms were compared on roughly 40% as many decisions
  as the OpenAI arms. Since 2026-09-20 a no-decision cycle records no
  predictions, and the Learning page shows each arm's no-decision
  share per week. The already-journaled rows were moved out of the
  learning data on 2026-09-20 (8,187 predictions plus 42,519 outcome
  rows hanging off them, into `*_no_decision` tables in the same
  journals — reversible; CHANGELOG 2026-09-20), so HOLD figures read
  AFTER that date are clean, and any read before it are not. What the
  move cannot restore is the missing decisions: the Gemini arms still
  have ~21% fewer scored cycles than they were given.
- **2026-08-24 → 2026-09-16: the shadow grader was blind.** It did not
  understand the batched `{"verdicts": [...]}` schema the 08-23
  vendor-fair build introduced, so 0 of 37,941 shadow calls were
  graded live for 23 days. Nothing was lost — the raw responses were
  stored, and 31,733 comparisons were backfilled on 09-16 — but any
  /shadow reading taken before 09-16 was empty, not neutral. The
  extractor is now tied to `_verdicts_schema()` by test. Multi-
  candidate set disagreements gained per-symbol outcome scoring the
  same day.
- **~3,200 shadow calls on profiles 229–234 died with Gemini 429
  quota errors** in the same window (OPEN_ITEMS); those comparisons
  do not exist, so Gemini-as-challenger coverage is thinner on the
  OpenAI-primary replicates than the reverse.
- **Six strategies have never fired** (quarantined 2026-09-16), so
  every arm is effectively running 19 strategies, not 25 — equal
  across arms, so it does not bias the comparison, but it bounds what
  "the system" means in this experiment.
- **Books are penny-exact.** The equity-identity alarm after the
  09-18 expiry (p229/230/231) was an audit defect, not a books
  defect (CHANGELOG 2026-09-19); no journal row was altered.
- **Spend is running above plan:** ≈ $3.56/day measured 09-12→18
  (primary ≈ $1.86, shadow ≈ $1.70) against the ≈ $68/month planned
  in [25](25_MODEL_SELECTION_AND_LEARNING_PLAN.md) §1.5.

### Return points
- Reset applied 2026-08-23 18:16 UTC (re-run the same evening with the
  four-arm manifest after the operator's incumbent ruling; no trades
  had occurred); first trading session 2026-08-24. Profiles 229–240 on
  Alpaca paper accounts 61/62/63 (`08-24-acct-1..3`), $250K each, one
  replicate of every arm per account (decision D2); certified clean.
  Experiment 1's learning data (incl. shadow rows):
  `backups/predictions_archive/<pid>/exp1_final_20260823/` for pids
  207–219 (170,536 rows) plus the full pre-wipe DB copies in
  `backups/pre-orphan-cleanup-20260823T174011Z/`.
- Start tag: `exp2-learning-phase1-start` (the commit that recorded
  the reset).
- Decision and progress logs: [25](25_MODEL_SELECTION_AND_LEARNING_PLAN.md) §4–5.

### What it found — the 2026-11-02 post-mortem (numbers from the journals, the broker and the scheduler log)

**1. The experiment had quietly collapsed to one arm.** On 2026-11-02
the twelve books were checked for the date of their last AI decision:

| Arm | Books | Last decision | Cause (scheduler log) |
|---|---|---|---|
| `gpt-4.1-nano` | 229–231 | 10-12 / 10-23 / 10-23 | OpenAI shut the model down 2026-10-23 (announced 04-22); every call 404 `model_not_found` — 154 failed cycles since 10-20 |
| `gpt-5.6-luna` | 232–234 | 11-02 (healthy) | — |
| `gemini-3.5-flash-lite` | 235–237 | 09-25 / 10-21 / 09-25 | Google 402 `RESOURCE_EXHAUSTED: prepayment credits depleted` — 462 failed cycles since 10-20; Google also deprecated the model itself on 2026-10-08 (routed to 3.6 Flash) |
| `gemini-3.7-flash` | 238–240 | 10-22 / 10-22 / 09-25 | same 402; model deprecated 2026-10-08 (routed to 3.8 Flash) |

Nine of twelve books made no decision for two to six weeks while their
stops and sweeps kept running, so the books looked alive. Nothing
probed the AI providers (the health probe covered Alpaca and yfinance
only), the model picker's "available" badge read the vendor's list-
models endpoint, which still returned `gpt-4.1-nano` three weeks after
it stopped answering, and no alert fires when a book goes N cycles
without a decision. **This is the finding that outranks every other
one here**: the comparison the experiment existed to make was not
being made from late September on, and the operator learned it from a
P&L review, not from the system. The fix is Experiment 3's first
deliverable (provider health probe, deprecation lookahead, decision-
flow watchdog, credit runway — CHANGELOG 2026-11-02). Shipping it
exposed a second blind spot: the data-source health probe installed
after the 2026-05-15 Alpaca-key incident had been dispatched from
inside the once-a-day snapshot block, so "every 10 minutes" had
really been once a day at the close for six months; both probes now
run every cycle (CHANGELOG 2026-11-02, "Health probes run every
cycle").

**2. P&L: −$67,349 on $3M since 08-24 (SPY −0.8%), 2 of 12 books
positive** — but the loss was the exit policy, not the entries.
Realized P&L by exit reason across all books: trailing-stop fills
**−$122,421** (565 long exits, median 0.1 days after entry, 78% of the
stopped longs had been up >1% first, price back +1.8% in the
position's favor by the next close in 69% of a sample), options
−$17,045 (single-leg long calls/puts −$16.9K of it, 28% win), **the
AI's own exit decisions +$74,201**, fees/dividends −$612. Entry
execution paid $43,025 of gross slippage in ten weeks with market
orders (p90 +26 bps against a 3.9 bps median spread). All three were
changed on 2026-11-01/02 before the reset: breakeven-then-trail stops,
no naked long premium, marketable-limit entries (CHANGELOG).

**3. The directional signal exists.** 10,532 AI-selected directional
predictions resolved (outcome = raw underlying move at the horizon,
sign-adjusted): **+0.25% per call**; bearish calls +1.17% at 51% win
(n=4,830), bullish −0.53% at 43% (n=5,702). Entry hit rate 41–53% by
month, no arm trending up. The signal reached the predictions and was
destroyed between prediction and realized P&L.

**4. Entry-quality levers, measured on the same 10,532 predictions
(all shipped before the reset, CHANGELOG 2026-11-02):**
- Strategies that lose on their own direction: `earnings_drift` 23%
  win −3.76%/call (n=64), `insider_selling_cluster` 34% −1.12% (n=1,158,
  the largest single drag), `vol_regime` 50% −0.92% (n=400). Removed
  from the active set (`strategy_policy.OPERATOR_DISABLED_STRATEGIES`).
  Winners: `batch_ai` (the AI's own pick, no screener vote) +1.81%
  (n=1,565), `volume_spike` 61% +0.70% (n=1,712), `mean_reversion`
  +0.39%. `volume_spike` is marked in the candidate line.
- **Confidence is inverted**: the 80–89 bucket is the worst (40% win,
  −0.99%), the 60s the best (+0.61%). Yet `confidence_sizing` scaled
  positions 1.5× at 80+ and 0.7× below 60, and the confidence gate
  blocked entries below the profile floor. Both removed from decisions
  (sizing flat, gate inert); confidence is still recorded.
- **Specialist VETOs were advisory in practice.** 955 traded SHORTs
  carried an ensemble verdict of VETO from a veto-authorized
  specialist (`adversarial_reviewer` 633, `risk_assessor` 556; −0.28% /
  −0.27% per call vs +0.25% overall). Cause: the pre-selection veto
  exempted "held exits", `held_symbols` is the shared Alpaca account's
  positions (three books per account), so a bearish candidate on a
  symbol any sibling book held long passed as an exit and the AI then
  opened a SHORT. Fixed with a post-selection gate: a vetoed symbol can
  only become an exit (`SPECIALIST_VETO` drop code).
- **The regime classifier called 88% of predictions "bull"** through a
  flat quarter (51,315 bull / 6,101 sideways / 714 bear) because
  `detect_regime` read "price above a rising SMA50" as bull; after a
  rally the SMA50 keeps rising for weeks while price goes nowhere.
  Replaced by a 40-bar return rule (±3%) shared with the short-slate
  throttle, which also called "strong bull" and suppressed routine
  shorts — the profitable side.
- **The "require a meta score" lever was a time artifact**: every
  unscored prediction (3,319) is from August, before any book had a
  trained meta-model; every prediction since September is scored. No
  change made; recorded so it is not re-proposed.
- Meta-model AUC by book 0.50–0.72; specialist cautions do not
  discriminate (46–47% win at 0 and at 3+ cautions). The owned-model
  track (P4.3 batch 5) waits for a corpus produced under the corrected
  policies.

**5. Spend:** $66.44 total since 08-24 across all AI calls; $39 in
September with all twelve books alive (per book: nano $0.7–0.9, luna
$2.6–2.9, 3.5-flash-lite $3.0–3.8, 3.7-flash $4.5–8.2). The plan's
"$3.56/day" alarm in the validity notes was the shadow burn, since
stopped. Budget was never the constraint; dead credentials were.

**6. Model landscape at retirement (verified 2026-11-02 against the
vendors' pricing and deprecation pages; see [25](25_MODEL_SELECTION_AND_LEARNING_PLAN.md) §1.6):**
`gpt-4.1-nano` retired 10-23; `gpt-5-nano` / `gpt-5-mini` / `gpt-5`
retire 12-11; `gemini-3.5-flash` / `gemini-3.7-flash` deprecated 10-08;
the catalog had none of GPT-6 (Astra / 6.1 Sol / 6.1 Terra / 6-luna),
Gemini 4 Argon, Claude 5.5 (Haiku / Sonnet / Opus) or Claude Fable 5.1,
nor the current open-weight tier (DeepSeek V4.1 Flash, Kimi K2.7, GLM
5.3, Qwen 3.8).

### Why it is retired
Its first question (beat the incumbent) can no longer be asked — the
incumbent no longer exists. Its arm comparison stopped being made in
late September for three of four arms. Its P&L was dominated by an
exit policy that has since been replaced, so pre/post books are not
comparable. What it produced carries forward: the platform fixes
above, the measured entry-quality levers, the model-landscape audit,
the warning-system requirement, and 10,532 resolved directional
predictions scored on raw underlying moves, which the exit and
execution changes do not touch.

### How to return to this exact moment
- **Git:** tag `exp2-learning-phase1-final` (the commit that retires
  this entry).
- **Data:** per-profile DBs `quantopsai_profile_{229..240}.db`;
  archived by the reset script into `backups/predictions_archive/
  <pid>/exp2_final_<date>/` (step 1c); master DB snapshots
  `backups/quantopsai.db.YYYYMMDD-0500`.
- **Code at retirement:** `full_fresh_start_2026_08_24.py`,
  `create_experiment_profiles.py` at the tag (four Experiment 2 arms).
- **Attribution queries:** `scripts/exit_attribution.py`; the
  strategy / confidence / veto / regime numbers above are reproducible
  from `ai_predictions.features_json` (`ensemble_specialists`,
  `regime_at_prediction`, `strategy_type`, `meta_model_score`).

---

## Experiment 3 — Cost Ladder on a Corrected System — DESIGNED 2026-11-02 (start: at reset)

### What it is designed to answer
1. **Does cost buy returns?** Four models spanning a 10× price range
   ($0.10/$0.50 to $1/$5 per 1M tokens) and the independent
   Artificial Analysis intelligence index 32 → 51, all fed the same
   data, strategies, specialists and policies. Operator framing: "does
   a free or low-cost model perform the same as an expensive model
   because of the system we have built providing so much data into
   the decision making process?"
2. **Did the 2026-11 corrections move the P&L to where the signal is?**
   Experiment 2 measured +0.25%/call of directional signal and lost
   money anyway. With static-then-trailing stops, marketable-limit
   entries, no naked long premium, the three losing strategies off,
   confidence out of sizing, binding vetoes and a regime label that
   reads a flat market as flat, realized P&L should track the
   prediction-level signal. The incumbent arm (`gpt-5.6-luna`, the one
   model with a continuous record) is the bridge that makes the
   before/after readable on the same model.
3. **How much does the owned model help** — the fine-tune corpus now
   accumulates under the corrected policies; batch 5 trains on it
   (docs/27) and, when it clears its gate, joins as an arm.

### Design
- **Four arms × three replicates**, $250K each, one replicate of every
  arm per paper account (unchanged from Experiment 2 — the structure
  that proved stable). Only `ai_provider` / `ai_model` differ (D9):

  | Arm | Model | Vendor | $/1M in / out | Index | Est. $/month (3 books) |
  |---|---|---|---|---|---|
  | floor | `claude-haiku-5-5` | Anthropic | 0.10 / 0.50 | 36 | $3.30 |
  | open-weight | `deepseek-flash` (V4.1 Flash) | DeepSeek | 0.30 / 1.20 peak, half off-peak | 32 | $4.65 |
  | incumbent | `gpt-5.6-luna` | OpenAI | 0.20 / 1.20 | — | $7.05 |
  | top | `gpt-6.1-terra` | OpenAI | 1.00 / 5.00 | 42–51 | $37.50 |

  Projected ≈ $52/month at September's measured volume (7.5M in / 0.7M
  out per book, 1M out for the reasoning arm). **Hard ceiling $100/month
  fleet-wide** (D10), enforced by the cost guard, with a daily credit-
  runway projection.
- **Why not a frontier arm:** any $2/$10 model (GPT-6.1 Sol, Claude
  Sonnet 5.5, Gemini 4 Argon) costs ≈ $75/month as a three-replicate
  arm and Claude Opus 5.5 ≈ $150, each alone breaking the ceiling.
  Terra at max effort scores within a few index points of Sol at high
  effort, so the ladder still spans a frontier-adjacent model against
  a $0.10 one. Prompt caching (new in this experiment) is measured for
  a month; if the bill runs well under $50 the top arm can be revisited
  at the next reset, never mid-experiment.
- **Three vendors**, so a single vendor's deprecation or billing event
  cannot blind the whole ladder again.
- **Warning system in force before the first cycle** (D8): per-profile
  provider probe every 30 minutes classifying deprecated-model / credits
  / quota / auth / outage; vendor deprecation lookahead at 60/30/7 days;
  decision-flow watchdog (no successful decision in N market hours);
  credit-runway projection against the $100 ceiling. Email on first
  occurrence, daily while unresolved, dashboard banner.
- **Policies in force from day one** (CHANGELOG 2026-11-01/02): see
  Experiment 2 §4 above. All books, all at once — the operator's
  standing rule (rollouts are never staged; the books are one dataset).
- **Baselines virtual** (D6), **shadowing specialist-only** (D5), **pre-
  registered decision rule** (docs/25 §3) all carried forward.
- **Data expansion, self-built only** (D12): the operator does not pay
  for data subscriptions; sources are public endpoints, cached locally,
  each shipped as a deterministic checker plus a weightable signal so
  the self-tuner measures its hit rate. First tranche: SEC fails-to-
  deliver, Reg SHO threshold list, Form 144, N-PORT fund holdings, CBOE
  VIX term structure, own-universe market breadth, IPO lockup calendar.

### Why this design
Experiment 1 compared small models and could not score the apex call;
Experiment 2 compared small/mid models and lost three arms to dead
credentials while an exit policy hid the signal. Experiment 3 keeps
the replicate structure that worked, puts the monitoring in first,
spans cost instead of vendor, and runs on policies chosen from the
measured decision data rather than assumed.

### Return points
- Design decisions D8–D14 in [25](25_MODEL_SELECTION_AND_LEARNING_PLAN.md) §4.
- Reset script `full_fresh_start_2026_11_03.py`; manifest
  `create_experiment_profiles.py` (Experiment 3 arms); start tag
  `exp3-cost-ladder-start` (set when the reset is applied).
