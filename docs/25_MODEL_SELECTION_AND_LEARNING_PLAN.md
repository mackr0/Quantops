# 25 — Model Selection & Learning Plan (living document)

**Status:** ACTIVE · opened 2026-08-21 · owner MacKenzie Smith ·
history and rationale in [26 — Experiments Register](26_EXPERIMENTS.md)
**Rule for this document:** it is the single place the plan lives. Every
step has a checklist; items are checked off (`[x]`) with a date and the
commit or action that closed them. Decisions are recorded in the
Decision Log, never re-litigated silently. When something is learned
that changes the plan, the plan is edited here, with a dated note.

---

## 0. Why this plan exists (the finding, dated 2026-08-21)

After six weeks of the 2026-07-08 cohort, an audit of the learning and
model-comparison machinery found:

1. **No model learns.** No LLM weights change. What adapts is the
   scaffolding around the model (self-tuner parameters, the meta-model's
   confidence re-scoring, prompt context). Shadow arms receive the
   identical prompt, so prompt-borne learning is shared; the *feedback*
   loops are driven only by the primary's outputs.
2. **The model comparison could not decide anything.** The shadow
   evaluation grades specialist verdicts only — the apex trade-selection
   call (`batch_select`) returns a trade-set string the grader cannot
   score — so the most important decision has never been compared. And
   the cohort ran one profile per arm, which can never reach
   statistical significance.
3. **The learning loops were mostly churn.** 429 self-tuner changes with
   no measurable improvement (win rate 37.0% → 37.9% around changes);
   many moved non-binding knobs (`max_total_positions` oscillating
   999→749→562→976 while `max_position_pct` actually caps positions).
   `learned_patterns` produced zero rows in six weeks. The
   `ai_model_auto_tune` toggle on the Settings page is stored but never
   read. Confidence ≥70 beat <70 in only 2 of 6 weeks. The meta-model's
   high-score half beat its low-score half in 1 of 3 weeks.
4. **What was real:** nano (`gpt-4.1-nano`) beat the primary on
   specialist verdicts (+1.27 pts/decision, n=406, p=0.001; direct
   head-to-head vs haiku +2.76 pts/unit, p=0.0005) — but the edge is
   concentrated where the primary was bearish/blocking, during an
   up-month (SPY +3.7%). Better judgment vs bullish tilt is not yet
   separable.

The platform itself (execution, reconciliation, measurement) is sound;
the experiment design and the learning loops are what change.

---

## 1. Model landscape (verified 2026-08-21 against provider pricing pages)

### 1.1 Measured volume per profile per month (30 days to 2026-08-21)

| Purpose | Calls | Input tokens | Output tokens |
|---|---|---|---|
| `batch_select` (apex trade selection) | ~530 | ~4.6M (78%) | ~123K |
| ensemble specialists (7) | ~1,330 | ~1.2M | ~300K |
| other (transcripts, SEC diffs, proposals) | ~30 | ~22K | ~2K |
| **Total per profile-month** | **~1,900** | **~5.9M** | **~0.43M** |

Fleet primary spend (13 profiles, gemini-3.1-flash-lite): **$27.5 / 30d**.
Shadow spend: $147 / 30d (haiku $134 of it).

### 1.2 Candidate models — price and cost *at our measured volume*

Prices are standard (non-batch, uncached) per 1M tokens from each
provider's official pricing page. "Cost / profile-month" applies our
measured 5.9M in / 0.43M out. Claude 4.7+ models use a tokenizer that
produces ~30% more tokens for the same text — their effective column
reflects that.

| Model (API id) | Tier | In $/M | Out $/M | Cost / profile-month | Notes |
|---|---|---|---|---|---|
| **OpenAI** | | | | | |
| `gpt-5-nano` | small | 0.05 | 0.40 | **$0.47** | cheapest modern option |
| `gpt-4.1-nano` | small (legacy) | 0.10 | 0.40 | $0.76 | the only arm with measured evidence; absent from OpenAI's current-models page — deprecation risk |
| `gpt-5.6-luna` | small | 0.20 | 1.20 | $1.70 | current-generation cost-optimized; 1.05M context |
| `gpt-5-mini` | small+ | 0.25 | 2.00 | $2.34 | |
| `gpt-5.4-mini` | mid− | 0.75 | 4.50 | $6.36 | |
| `gpt-5.6-terra` | mid | 2.00 | 12.00 | $16.96 | "balances intelligence and cost" |
| `gpt-5.6-sol` | frontier | 4.00* | 20.00* | $32.20* | *promo through 2026-11-21 (list $5/$30 → $40.40) |
| **Google** | | | | | |
| `gemini-3.1-flash-lite` | small | 0.25 | 1.50 | $2.12 | **current primary** |
| `gemini-3.5-flash-lite` | small | 0.30 | 2.50 | $2.85 | "fastest, most budget-friendly" current gen |
| `gemini-3.7-flash` | mid | 0.75† | 3.75† | $6.04† | †through 2026-12-31, then $1.50/$7.50 → $12.08; positioned for "agentic workflows" |
| `gemini-3.1-pro-preview` | frontier | 2.00 | 12.00 | $16.96 | **preview** — can change mid-experiment; ≤200K prompts |
| `gemini-2.5-flash-lite` | small (legacy) | 0.10 | 0.40 | $0.76 | reported retirement 2026-10-16 (third-party source; official page lists no date) |
| **Anthropic** | | | | | |
| `claude-haiku-4-5` | small+ | 1.00 | 5.00 | $8.05 | measured: WORSE than primary (p=0.031), most expensive small model |
| `claude-sonnet-5` | mid | 2.00 | 10.00 | $16.10 → **~$20.9** w/ tokenizer | $2/$10 confirmed permanent (was intro) |
| `claude-opus-5` | frontier | 5.00 | 25.00 | $40.25 → **~$52.3** w/ tokenizer | |
| `claude-fable-5` | frontier+ | 10.00 | 50.00 | $80.50 → ~$104.7 w/ tokenizer | thinking always on; 30-day data retention required |

Prompt caching (all three vendors bill cache reads at ~10% of input)
could cut the batch-select input bill substantially **if** the prompt
prefix is made stable; the 2026-07-02 evaluation found the current
prompt too volatile to cache. Treat caching as upside, not baseline.

### 1.3 How to read capability for THIS workload

The job is structured judgment over numeric/market context with JSON
output, ~1,900 calls per profile per month, latency-tolerant (5-minute
cycles). What matters, in order:

1. **Reasoning depth on ambiguous evidence** — the apex call weighs
   dozens of candidates against a book; this is where tiers separate.
2. **Calibration honesty** — the confidence number feeds the gate and
   the meta-model; a model that inflates confidence poisons its own
   profile's learning.
3. **Instruction following / schema reliability** — specialist verdicts
   must parse; a parser failure is a silent HOLD.
4. **Stability for 12 weeks** — no preview models, no deprecation-risk
   models as primaries.
5. Cost is last: on a $250K virtual book, $50/month is 0.02%.

The experiment so far compared three **small-tier** models against each
other. It has never tested whether a mid or frontier tier buys returns.
That is the question the new design must answer.

### 1.4 Where today's spend goes (30 days to 2026-08-21)

| Line | $/30d | Comment |
|---|---|---|
| Primary, 13 profiles (gemini-3.1-flash-lite) | $27.5 | ~$2/profile |
| Shadow: claude-haiku-4-5 | **$134** | measured WORSE than primary (p=0.031) |
| Shadow: gpt-4.1-nano | $9 | the informative arm |
| Shadow: adversarial_v2 variant | $3 | |

Haiku is 77% of the bill and lost. **Immediate action (operator):
remove haiku from every profile's shadow list** → ~$40/month before
any redesign.

### 1.5 Recommended arms — Phase 1 (decision pending, D1)

Budget constraint (operator, 2026-08-21): must cost **less than
today's ~$175/month**. A first draft with Sonnet 5 / Opus 5 arms and
full cross-shadowing came to ~$675/month and was rejected on cost.

Two levers make a replicated 3-arm design affordable:

1. **Cross-shadow the specialist purposes only.** Trade selection
   (`batch_select`) is 78% of tokens, but each arm makes REAL
   trade-selection decisions on its own three profiles — that is the
   live A/B. Shadowing it as well pays twice for the same answer.
   Specialist verdicts (22% of tokens) are where same-call shadowing
   earns its keep (the nano finding came from there).
2. **No frontier arm in Phase 1.** A frontier primary is ≥$52 × 3
   replicates regardless of design. It waits for Phase 2, funded by
   what a mid-tier arm shows.

| Arm | Model | Primary (3 profiles) | Specialist-only shadow on the other 9 | Question it answers |
|---|---|---|---|---|
| **Incumbent**, OpenAI | `gpt-4.1-nano` | $2.28 | $2.25 | Experiment 1's measured winner — the bar every other arm must clear on real trades |
| Small, OpenAI | `gpt-5.6-luna` | $5.10 | $5.49 | nano's successor: does the new generation keep the edge (and hedge nano's deprecation) |
| Small, Google | `gemini-3.5-flash-lite` | $8.55 | $7.56 | vendor vs vendor at equal tier |
| Mid, Google | `gemini-3.7-flash` | $18.12 | $18.72 | **does a higher tier buy returns** — same vendor as 3.5-lite, so it isolates tier |
| Baselines: BuyHold, Random ×10 (virtual) | none | $0 | — | skill vs luck |
| **Total** | | | | **≈ $68/month** (vs ~$175 before) |

(Revised 2026-08-23 from a three-arm draft after the operator's
ruling that the incumbent must be an arm, not a shadow.)

Specialist-only shadow cost per profile-month at measured specialist
volume (1.27M in / 0.30M out): luna $0.61 · 3.5-flash-lite $0.84 ·
3.7-flash $2.08 · sonnet-5 ~$7.2 · opus-5 ~$18.

Caveats: `gemini-3.7-flash` doubles in price on 2027-01-01 (total
→ ~$80/month if the run extends past December — still under today).
Alternative mid arm `claude-sonnet-5` (stronger calibration, permanent
price, different vendor): same design ≈ $91/month.

**Phase 2 (after Phase 1 decides):** add one frontier arm
(`claude-opus-5` or `gpt-5.6-sol`) only if the mid arm beat the small
arms — otherwise the frontier question is moot.

**Uncounted upside:** trade-selection prompts are ~8,700 tokens, mostly
stable context; every vendor bills cache reads at 10% of input. A
cache-stable prompt prefix (Step 1.11) could cut the largest line item
by more than half on every arm.

---

## 2. The five steps

Each step: goal → why → checklist → done-when. Check items off here.

### Step 1 — Make the model test real (replicates; only the model differs)

**Goal:** three arms × three profiles, identical capital and settings,
fresh-started together, plus the BuyHold and two Random baselines.
**Why:** one profile per arm can never reach significance; matched
replicates can. The shadow layer stays on cross-wise so every arm is
also scored on identical calls.

Prerequisites (code):
- [x] 1.1 Update `ai_providers.PROVIDERS` and `ai_pricing.PRICING` with
      the current model set and verified prices; fix opus-4-6 to
      $5/$25; mark legacy ids; `PRICES_VERIFIED_ON`. Done 2026-08-23
      (pinned: every registered model priced; defaults current).
- [x] 1.2 **Equalize the output path across vendors.** One `schema`
      through `call_ai` → each vendor's native structured mode; shadows
      get the same schema; ensemble's Anthropic-only fork and the
      text-parser branch removed. Done 2026-08-23
      (`tests/test_structured_output_vendor_fair_2026_08_23.py`).
- [x] 1.3 `batch_select` gradeable in shadow: trade sets exploded per
      symbol and graded with the forecaster rules. Done 2026-08-23
      (register: `calculation_verification/shadow.md`).
- [ ] 1.4 Remove the dead `ai_model_auto_tune` toggle from Settings (or
      implement it — decision D3). A control that does nothing must not
      stay on the page.
- [ ] 1.5 Per-profile shadow config: confirm each arm-profile can carry
      its own `shadow_models` list and provider keys (it can today —
      verify the Settings save path round-trips three arms).

Configuration (operator, with Claude driving):
- [x] 1.6 Provider keys installed per profile by the reset script
      (primary key + shadow-key map for the other vendors). Done
      2026-08-23.
- [x] 1.7 D1 (arms) and D2 (capital) decided. Done 2026-08-23.
- [x] 1.8 Fresh start applied 2026-08-23 17:40 UTC
      (`full_fresh_start_2026_08_24.py --apply`): learning data archived
      first (170,536 rows), certify PASS on funding/drift/reconcile/
      decomposition.
- [x] 1.9 Profiles 220–228 configured from the manifest (identical
      except model; cross-shadow lists verified in the DB); baselines
      are the 11 virtual benchmarks. Done 2026-08-23.
- [x] 1.10 Pre-registered (Section 3). Start date: 2026-08-24.
- [ ] 1.11 Cache-stable prompt prefix for `batch_select`: move volatile
      content (timestamps, per-cycle ids, book state) AFTER a frozen
      system/instruction prefix so every vendor's cache-read rate (10%
      of input) applies; verify with `cache_read`/`cached_tokens` > 0 in
      the cost ledger. Largest single cost lever; re-evaluates the
      2026-07-02 "too volatile to cache" finding against the new prompt.
- [x] 1.12 **Virtual baselines (D6).** `virtual_benchmarks.py`: holdings
      selected once (sha256 seed), marked to market daily from Alpaca
      bars, dividends credited from corporate-action data, Random × 10,
      series on the comparative chart, no orders / reconcile / conduit
      capital; created by the reset script (step 4b), no purchase
      steps. Done 2026-08-23 (register:
      `calculation_verification/benchmarks.md`). Live verification
      after the first daily mark: ____.
- [x] 1.13 Reset tooling staged: `full_fresh_start_2026_08_24.py`
      (keys from env only; per-provider AI + shadow keys; archives
      Experiment 1's learning data before the wipe; creates the
      virtual benchmarks) and the Experiment 2 manifest in
      `create_experiment_profiles.py` (9 arm-profiles, verified
      identical except model). Done 2026-08-23. Runs when the operator
      supplies the three new paper accounts (1.6–1.9).
- [x] 1.0 **Immediate, before any of the above:** remove
      `anthropic:claude-haiku-4-5-20251001` from every profile's shadow
      list (77% of spend; measured worse than primary). Done
      2026-08-23 on prod, profiles 207–219 (operator decision).

**Done when:** 12 profiles trading from equal capital on the same day;
/shadow shows three arms with every purpose including batch_select
graded; the register (`calculation_verification/shadow.md`) updated.

### Step 2 — Define "learning" as a curve you can see

**Goal:** a Learning Scoreboard page: per arm, per week, out of sample —
calibration (confidence ≥70 vs <70 hit rate, and a Brier score),
directional hit rate, HOLD quality, and return in excess of SPY — with
the three replicates of each arm shown together.
**Why:** "is it learning" must be a slope on a chart, not a feeling.
"Better model" must be one arm's lines above the others across all
three replicates.

- [x] 2.1 Register written first: `calculation_verification/learning.md`.
      Done 2026-08-23.
- [x] 2.2 `/learning` (`learning_scoreboard.py`, `templates/learning.html`,
      nav link): per arm × ISO week — hit rate, high/low-confidence hit
      rate, Brier, HOLD quality, mean move, weekly equity return with
      replicate min/max, SPY, excess; virtual-benchmark band; thin-week
      flag; never 0% for unmeasured. Done 2026-08-23.
- [x] 2.3 Tuner scorecard per arm (improved / worsened / unchanged /
      pending, improved share of judged) on the page. Done 2026-08-23.
      (Tuning-OFF replicates: decision D4 still pending.)
- [x] 2.4 Engine fixtures + Flask-client smoke test + template empty-state
      and route/nav structural tests. Done 2026-08-23.

**Done when:** the page renders for the new cohort and the operator can
answer "is arm X improving week over week" from it alone.

### Step 3 — Shrink the tuner to levers with real sample sizes

**Goal:** the self-tuner changes only knobs that have a measured
relationship to outcomes at a minimum sample size; everything else is
retired.
**Why:** 429 changes produced no measurable improvement; most moved
knobs that don't bind or reversed themselves by auto-expiry.

- [x] 3.1 Keep: `EVIDENCE_BACKED_OPTIMIZERS` (signal weights, meta
      pre-gate, false-negative floor lowering, trade-count loosener,
      auto-expiry, RSI thresholds, short/options toggles, upward sizing).
      Done 2026-08-23 (`SELF_TUNER_MODE=evidence` default).
- [x] 3.2 Retire: everything else — including `max_total_positions`
      (refused at the apply choke point on every path), ATR-TP
      ping-pong, per-symbol/regime overrides, drawdown tightening,
      fast-lane retirement, and `_optimize_prompt_layout` (arms must see
      identical prompts). Done 2026-08-23.
- [ ] 3.3 One gate for every remaining adjustment: minimum sample n,
      minimum effect size, and a recorded expected-vs-observed outcome
      that the Learning Scoreboard displays. (Partially covered: the
      allowlist keeps only large-sample levers; the explicit per-change
      n/effect gate and the expected-vs-observed record are still to
      build — with the Scoreboard, step 2.3.)
- [ ] 3.4 Run one arm-replicate per model with tuning OFF? — decision D4
      (costs a replicate; alternative is the before/after scorecard).
- [x] 3.5 `docs/17_SELF_TUNER_GUARDRAILS_AND_RAG.md` carries the
      evidence-mode section. Done 2026-08-23.

**Done when:** the tuner's change log shows only gated changes with
expected outcomes, and the non-binding knobs no longer appear in it.

### What counts as "learning" in this system (and what does not)

Operator question 2026-08-23: "are you fixing this so that the system
is actually learning instead of just moving levers and knobs?" The
test for learning is a **closed loop on the decider**: the thing that
makes the decision receives its own past outcomes, its decisions
measurably change, and the Learning Scoreboard shows the curve moving
in the right direction. Three mechanisms pass that test; one does not.

| Level | Mechanism | Changes what? | Status 2026-08-23 | Plan |
|---|---|---|---|---|
| 0 — knobs | self-tuner parameter moves | thresholds/caps around the model | 429 changes, no improvement, non-binding knobs | **Step 3 retires all but evidence-backed levers.** Not learning. |
| 1 — statistical | meta-model: a gradient-boosted classifier fit per profile on prediction features → outcomes (`meta_model.py`) | the confidence the gate sees | real learning, but not yet discriminative (high-score half won 1 of 3 weeks) | kept, judged by the Scoreboard (4.3); retired if it never separates |
| 2 — in-context | the prompt carries the profile's own track record (calibration by band/signal/sector/strategy; `learned_patterns`) | the model's decisions, every cycle | `learned_patterns` = 0 rows ever; calibration block absent | **Step 4.1–4.2 builds it.** This is the learning a frozen-weight model can do. |
| 3 — weights | fine-tuning on the system's own resolved predictions (`docs/20_FINETUNE_PHASE_4B1_INCREMENTAL.md`; `finetune/dataset_builder.py` + `model_registry.py` shipped 2026-05-21) | the model itself | designed and foundation shipped; NEVER activated — gated on corpus accumulation, which the 07-08 reset restarted | **Step 4.5 activates it** once the corpus threshold is met and the chosen arm's vendor supports tuning |

Level 3 is the only mechanism that changes the model; Level 2 is the
only one that changes decisions immediately; Level 1 is honest
statistics; Level 0 is what the operator rightly called "moving levers
and knobs." The plan keeps 1–3 and cuts 0 to the few levers with real
sample sizes.

### Step 4 — Turn on the learning an LLM can actually do (in-context, then weights)

**Goal:** every cycle's prompt carries the profile's own track record,
the machinery that was supposed to do this actually runs, and the
fine-tune path that was designed in May is activated when its corpus
exists.
**Why:** a frozen-weight model learns one way — by being shown what it
got right and wrong. `learned_patterns` has never written a row.

- [x] 4.1 Diagnosed: the post-mortem (and three other weekly tasks) was
      gated on `weekday() == 6`, but the fleet sleeps weekends, so it
      never ran (prod marker: 2026-07-26). Weekly tasks now run by
      marker age on any day (`_weekly_task_due`); structural test
      forbids Sunday gates. Done 2026-08-23. (The post-mortem still
      writes only on a bad week by design — the always-on learning is
      4.2.)
- [x] 4.2 `calibration_block.py` — the profile's own resolved record in
      every batch prompt: win rate and mean move by confidence band,
      call family, strategy, regime; n on every number; n<10 buckets
      and <20-row profiles stated, never 0%. Done 2026-08-23 (register:
      `calculation_verification/ai.md`).
- [ ] 4.3 Keep the meta-model, judged by the Scoreboard against its own
      history (high-score half must beat low-score half on a sustained
      basis, or it is retired).
- [x] 4.4 Prompt-side numbers registered in
      `calculation_verification/ai.md`. Done 2026-08-23.
- [ ] 4.5 **Weight-level learning (docs/20 Phase 4b.1):** (a) verify the
      fine-tune corpus on prod — `predictions_archive/` after the 07-08
      reset plus the new cohort's resolved predictions — against the
      docs/20 §17 activation threshold; (b) verify which of the chosen
      arms' vendors support supervised fine-tuning of that model
      (`gpt-5.6-luna`, `gemini-3.5-flash-lite`, `gemini-3.7-flash`) —
      the May design assumed `gpt-4o-mini`; (c) build the still-missing
      `training_runner` / `evaluator` / `inference` pieces; (d) run the
      fine-tuned model as a FOURTH arm on replicates (never a silent
      swap), scored by the same Scoreboard and decision rule.
      Sub-items are tracked in docs/20; this item is done when a
      fine-tuned arm is trading on replicates.

**Done when:** the prompt diff shows the calibration block populated
from live data, `learned_patterns` has rows on every active profile,
and the fine-tune path is either running as an arm or has a dated
reason in the Decision Log why not yet.

### Step 5 — Decide by a rule set now, not a feeling later

**Goal:** at the pre-registered horizon, promote the winning arm as the
primary everywhere; the others stay as shadow challengers; /shadow
becomes a standing leaderboard and "promote the challenger" a
procedure.

- [x] 5.1 Horizon set: 12 weeks from 2026-08-24 (read 2026-11-16;
      minimum read 2026-10-19). Recorded in §3. Done 2026-08-23.
- [x] 5.2 Primary metric: mean excess return vs SPY across the arm's
      three replicates, with the replicate spread shown. Tie-breakers:
      calibration (Brier), then cost.
- [x] 5.3 Significance: bootstrap on weekly excess returns pooled across
      replicates, p < 0.05; if no arm clears it, extend 4 weeks and
      re-read — never pick from noise.
- [x] 5.4 **Promotion without a reset** (operator requirement
      2026-08-23: switch primaries in production without halting or
      starting the brains over). Every prediction now carries the
      model that made it (`ai_predictions.ai_provider/ai_model`); the
      track-record block, meta-model training and the Scoreboard scope
      to the profile's CURRENT model and state other-model history
      instead of blending it; `model_promotion.promote(profile_id,
      provider, model)` swaps primary ↔ shadow and keys in one audited
      write (activity log `model_promoted`), never touching positions
      or orders; the profile keeps its book, history and tuner state and
      trades on the next cycle. Done 2026-08-23. Rehearsal on a live
      profile: ____ (after the horizon read).
- [ ] 5.5 Post-decision: the losing arms remain shadows on the winner's
      profiles so the leaderboard keeps running.

**Done when:** a dated decision entry exists in the Decision Log with
the numbers that drove it.

---

## 3. Pre-registered decision rule (fill in before the first cycle)

- Start date: **2026-08-24** (first trading session after the
  2026-08-23 reset; tag `exp2-learning-phase1-start`) · Horizon: **12
  weeks** (read 2026-11-16; minimum read 8 weeks, 2026-10-19) · Arms:
  **gpt-4.1-nano (incumbent) / gpt-5.6-luna / gemini-3.5-flash-lite /
  gemini-3.7-flash**, 3 replicates each at $250,000, one replicate of
  every arm per paper account
- Primary metric: mean weekly excess return vs SPY per arm (3 replicates)
- Significance: bootstrap p < 0.05 on pooled weekly excess returns
- Guard: an arm whose three replicates disagree in sign is "undecided"
  regardless of the pooled mean
- Secondary: Brier calibration; directional hit rate; cost
- Nothing in this section changes after the start date except by a
  dated Decision Log entry explaining why.

---

## 4. Decision Log

| ID | Date | Decision | Rationale | By |
|---|---|---|---|---|
| D0 | 2026-08-21 | Budget ceiling: Phase 1 must cost less than today's ~$175/month | operator constraint; the $675 full-cross-shadow design was rejected on cost | MS |
| D1 | 2026-08-23 | **Arm set: `gpt-4.1-nano` / `gpt-5.6-luna` / `gemini-3.5-flash-lite` / `gemini-3.7-flash`**, 3 replicates each (~$68/mo) | operator: the incumbent must be an arm — nano is Experiment 1's measured winner, and a new model is only better if it beats nano on real trades; Luna stays as the hedge against nano's deprecation. (Superseded the three-arm draft that had nano as a shadow only.) | MS |
| D2 | 2026-08-23 | Capital per replicate: **$250,000**, four arms per $1M account — replicate i of every arm on account i; benchmarks the same | each account fully allocated (the cash-parity audit requires Σ profile cash == broker cash; a three-arm $250K draft left $250K unowned per account and was flagged as orphan cash on every cycle), and a broker-account event hits all arms equally | MS |
| D3 | 2026-08-24 | **`ai_model_auto_tune` removed** (UI toggle, form handling, ctx field; column kept append-only) | operator: promote() covers it — model changes are evidence-based operator actions, never a tuner behavior. Distinct from Self-Tuning (`enable_self_tuning`), which stays ON in evidence mode | MS |
| D4 | pending | Tuning-OFF replicate per arm: yes/no | costs a profile per arm | MS |
| D5 | 2026-08-21 | Cross-shadowing: **specialist purposes only**, not `batch_select` | the replicates ARE the apex-call A/B; shadowing it pays twice (§1.5) | MS (proposed by Claude, pending confirmation) |
| D6 | 2026-08-23 | **Baselines tracked virtually (no broker), Random × 10 replicas** | static portfolios; removes wipe exposure, reset steps, $750K conduit use; free replicas give a real null band (item 1.12) | MS |
| D7 | 2026-08-23 | Target: Experiment 2 implemented before the 2026-08-24 open; the reset itself waits on new Alpaca paper accounts from the operator (or approval to reuse 55/56/57 in place) | operator directive | MS |

---

## 5. Progress Log

| Date | Step | What was done | Commit / action |
|---|---|---|---|
| 2026-08-21 | — | Audit completed; plan opened; model landscape verified | this document |
| 2026-08-23 | — | Budget ruling D0; Phase-1 design at ~$53/mo; arm set D1 approved; "what counts as learning" ladder added; fine-tune path (docs/20) folded in as 4.5 | cb22501, 3f8c328 |
| 2026-08-24 | — | First-session hardening: benchmark activation per cycle; submit-time pnl estimates removed from every stock exit writer; equity identity leg-derived (holds through fill windows); pnl-column corruption is its own finding. **New open items:** short-borrow cost accounting (the cover-time subtraction was already lost at recompute; needs a first-class cash treatment) and the option-close pnl estimate (kept as the fill machine's discriminator by the 07-22 design — the leg-derived identity is immune to it, but the discriminator role should eventually move off pnl). | 52711ad |
| 2026-08-24 | 5, D3 | D3 done (dead auto-tune toggle removed — promote() covers it); 5.1–5.3 marked done (pre-registration §3 was filled at the start); dashboard benchmarks show LIVE value (bulk read, 60s cache, kept at minute freshness by operator ruling — $0 cost, no rate-limit risk, zero AI involvement) with the column labeled **P&L %** to match the profile table (naming-consistency fix); identity verified clean 12/12 under both old and new semantics on day one. | 178ad40 |
| 2026-08-25 | — | Day-two incident: five long-option entries mislabeled `auto_closed_external` after our OWN exits filled (the 08-10 guard read only live rows; our own FILL activity counted as external evidence) — premiums vanished from cash/realized, equity drift +$185…+$4,550 on four profiles, cash phantoms on all three accounts, reconciled to the penny. Repaired fleet-wide (dry-run-first script), labeler race-proofed, and the **FILL-TRUTH INVARIANT** shipped at the accounting layer: a fill-bearing row never leaves cash or the realized FIFO regardless of status — future mislabels on any path are cosmetic, never money. 12/12 identity ≤$0.01 and cash parity at cents after. | 141e69c |
| 2026-08-27 | 4.5 | First fine-tune batches trained and honestly evaluated. Corpus: 46,583 archived resolved predictions → 100.0% prompt-joinable after the cycle-join fix → 4,120 labeled decisions → 1,369 cycle-grouped production-shaped examples. Batch 2 (Qwen2.5-7B-4bit LoRA, M2 Max, ~5h, $0): val loss 1.97→0.78. Held-out exam, 158 graded decisions: **adapter 38.6% vs base 37.3%** directional — NOT a meaningful win yet. What it DID learn: perfect output format (0 unparseable vs 9; base also emitted illegal option actions) and strong HOLD discipline (41/55 vs 22/55) — but overapplied as a cautious prior (70% of answers HOLD), costing bearish/bullish hits. Verdict: pipeline proven, model not yet promotable; **no hosting spend**. Next trigger: retrain at ~2× corpus (the four-arm fleet doubles it in ~3–4 weeks), consider 2–3 epochs + label-balance weighting; hosting decision re-opens on a clear base-beating eval. | bb3b424 |
| 2026-08-23 | — | Experiments Register opened ([26](26_EXPERIMENTS.md)); Experiment 1 retired and tagged `exp1-system-stability-final`; virtual-baseline item 1.12 + D6 added | cb22501… |
| 2026-08-23 | 1 | 1.0 haiku shadow pulled on prod; 1.1 registry/prices; 1.2 vendor-fair structured output; 1.3 apex call gradeable; D5 shadow scope; 1.12 virtual benchmarks; 1.13 manifest + reset script staged | this commit |
| 2026-08-23 | 3, 4 | tuner evidence mode (3.1, 3.2, 3.5); weekly tasks fixed (4.1); track-record prompt block (4.2, 4.4) | this commit |
| 2026-08-23 | 2 | Learning Scoreboard `/learning` (2.1–2.4) with register `learning.md` | 878c493 |
| 2026-08-23 | 1 | **Reset applied** (1.6–1.10), twice the same evening: first run archived Experiment 1 (170,536 rows; the archive was then deleted by a deploy and regenerated from the pre-wipe backups into `backups/predictions_archive/`); after the operator's incumbent ruling the reset was re-run with the four-arm manifest — profiles 229–240 on accounts 61/62/63 (`08-24-acct-1..3`), $250K each, keys installed, 11 benchmarks pending activation at the 2026-08-24 open, **CERTIFIED CLEAN** on all five checks. Pre-registration (§3) filled. | tag `exp2-learning-phase1-start` |

---

## 6. Constraints that do not move

- Profiles are virtual accounts; what belongs to a profile belongs only
  to that profile. No cross-profile concentration, no shared pool.
- Learning data physically separated from real-trade truth.
- Universe floors are operator-only; the tuner never touches them.
- Every displayed number has a register entry before it ships.
- Deploys only via `sync.sh`; CHANGELOG with every change; full gate
  before any claim of done.
