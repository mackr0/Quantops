# 27 — Fine-Tune Training Log

**The complete record of teaching a model this system's own judgment.**
One entry per training batch: what went in, what came out, what it
taught us. This is the document to reread when the owned model finally
takes a seat at the table — every batch below is a step on that road,
including (especially) the ones that mostly taught *us*.

Companion docs: the architecture and rationale live in
`20_FINETUNE_PHASE_4B1_INCREMENTAL.md` (§16.1 is the path we run);
the activation decision and verdict rows live in
`25_MODEL_SELECTION_AND_LEARNING_PLAN.md` (step 4.5). This log owns
the batch-by-batch history.

---

## The standing setup (established 2026-08-26)

| Piece | Choice | Why |
|---|---|---|
| Where training runs | The operator's M2 Max (64GB), never prod | $0 forever, no vendor, no cloud dependency; the droplet keeps trading while the Mac studies |
| Framework | Apple MLX (`mlx-lm`), LoRA adapters | Native Apple-Silicon; adapters are ~50–100MB artifacts that stack incrementally batch over batch |
| Base model | **Qwen2.5-7B-Instruct (4-bit)** | Equal-or-better than Llama-3.1-8B on structured/JSON tasks, fully ungated (no account, Apache license). Llama remains one flag away; decision closed 2026-08-26 |
| Teaching signal | Hindsight relabeling | Each resolved prediction becomes a flash card: the exact prompt the AI saw, answered with what would have been *correct* given the realized outcome. Losing entries relabel to HOLD; missed >5% moves relabel to the missed direction; ambiguous 2–5% moves are discarded rather than guessed |
| The iron rule | No look-ahead, asserted per row | Every label derives from an outcome resolved strictly AFTER the decision moment. A leaking row raises; it is never silently skipped |
| The exam | Held-out most-recent cycles, adapter vs its own untrained base, identical prompts | The only bar that matters: did our data make this exact brain better? Promotion (and any hosting spend) waits on a clear yes |
| Corpus source | `backups/predictions_archive` + live journals, cycle-joined | 46,583 resolved predictions from Experiment 1, 100.0% joinable to their full prompts; grows daily — all four Experiment-2 arms feed it (≈27,000 more labeled decisions by 2026-09-19; see "Where things stand" at the end) |

Training costs nothing but electricity and hours. The corpus is the
moat: nobody else can train on this system's prompts, fills, and
outcomes.

---

## Batch 1 — 2026-08-26 · "The pipeline lives (and confesses)"

**Corpus:** first real build — 4,120 labeled decisions (834 BUY /
2,386 HOLD / 900 SHORT), one example per prediction, 200 most-recent
held out.
**Run:** 600 LoRA steps, batch 1, 8K context, 5¾ hours. Peak memory
23.8GB. Validation loss **1.855 → 0.732 → 0.789**.

**What happened before the run even started** is half the story:

- The dataset builder — shipped in May, never activated — produced
  **zero examples** from a 46,583-row archive. Diagnosis: prompts
  moved to per-cycle storage on 2026-07-02 (6.15× dedup) and the
  builder still expected them per row. The cycle-join fix recovered
  100.0% of the corpus. *Lesson: activation is a test nothing else
  runs — "designed and never activated" means unverified.*
- First launch OOM'd the Metal GPU: 7B at 8K context needs gradient
  checkpointing on 64GB. One flag, permanent fix.

**Exam result:** adapter 30% vs base 24% on 50 decisions — but the
result was voided on inspection, and the inspection was the real
yield:

- The scorer graded `trades[0]` of a *batch* answer — an arbitrary
  candidate, not the labeled one — and scored every HOLD as a miss,
  when production semantics say an omitted symbol IS the HOLD.
- The 300-token generation cap truncated the base model's long batch
  answers mid-JSON, degrading its scoring to noise.
- With scoring corrected: 26% vs 24%, and the kept generations showed
  the deeper truth — **per-prediction training targets had taught the
  adapter to answer with a single pick** (44/50 answers were one trade
  or nothing) against prompts that ask for a whole batch. The corpus
  shape itself was teaching the wrong output convention — exactly the
  refinement the May design had flagged as "measure whether
  per-candidate framing underfits." Measured.

**Verdict:** pipeline proven end-to-end at $0; numbers void; corpus
shape wrong. Three real defects found and fixed that would have
silently poisoned every future batch. Best possible use of a first
batch.

---

## Batch 2 — 2026-08-27 · "It learned the right lesson too hard"

**Corpus:** same 4,120 labeled decisions, restructured to production
shape — **1,369 cycle-grouped examples** (one per cycle; the target is
the corrected action set for ALL of that cycle's labeled candidates,
HOLDs expressed by omission). 200 cycles held out.
**Run:** 600 LoRA steps, fresh adapter (batch-1's learned convention
was wrong, so no resume), ~5 hours. Validation loss **1.971 → 0.705 →
0.784** (both batches bottom near step 400 — noted).

**Exam result (158 graded decisions, fair rules, no truncation):**

| | Base (untrained) | **Adapter** |
|---|---|---|
| Directional accuracy | 37.3% | **38.6%** |
| Unparseable answers | 9 (plus illegal option actions) | **0** |
| HOLD accuracy | 22/55 | **41/55** |
| Bearish accuracy | 26/76 | 16/76 |
| Bullish accuracy | 11/27 | 4/27 |
| Answer mix | scattered | 70% HOLD |

**What it learned:** the job's output format, flawlessly — and the
system's most expensive historical lesson, *most setups deserve no
trade* (HOLD accuracy nearly doubled). **What it overlearned:** that
same caution as a blanket prior. 1.5 months of a single market regime
taught the base rate before the discrimination — it knows *that* to
hold, not yet reliably *when*.

**Verdict:** 38.6% vs 37.3% is a statistical tie, not a win. Under
the pre-set bar — clear base-beating eval before any spend — **no
hosting, no shadow arm yet.** The model stays local and free. This is
the honest machine doing its job: the same discipline that scores the
rented arms scored our own, and said "not yet."

---

## The doctrine these batches wrote (do not relearn the hard way)

1. **Cycle-grouped examples only.** One example per (cycle, prompt);
   targets carry every labeled candidate; HOLD = omission. Per-row
   targets teach a degenerate one-pick convention.
2. **Eval grades the labeled symbol's own entry**; omission is HOLD;
   generation length must fit a full batch answer (2,000 tokens);
   raw generations are kept in every report — an unexplained score is
   forensically worthless.
3. **An unparseable answer is a wrong answer** — live it would be one.
4. Gradient checkpointing always on; explicit `--max-seq-length` 8192
   (prompts run ~9–10K tokens; mlx's silent 2048 default truncates
   the candidate table).
5. Validation loss bottoms mid-run in both batches — evaluate the
   best checkpoint, not only the final step, starting batch 3.

## Batch 3 — 2026-08-27 · "The corpus was 8× bigger than we knew"

Launched the same day as batch 2's verdict, because two discoveries
made waiting absurd:

- **Options train now** (operator ruling: half the system doesn't sit
  out). A premium-based labeler grades each option decision by what
  its PREMIUM did — kept ≥ +20% → the action was right; lost ≥ 20% →
  the correct answer was no trade; ambiguous band skipped. Grounded in
  the measured distribution: the median archived option decision lost
  95.4% of premium, so the option corpus's first lesson is the
  expensive one — most of those setups deserved a pass.
- **The dedup bug.** The corpus builder deduplicated rows by bare row
  id — but every profile's journal counts 1, 2, 3 … so across 22
  archive dumps, later profiles' rows were silently swallowed as
  "duplicates." Fixing the key to (profile, id) recovered **34,157
  labeled decisions where batches 1–2 saw 4,120.** Batch 2's model
  learned from 12% of the data the system owns. (Found because the 14
  option premium-winners went missing from a rebuild — pulling that
  thread unraveled the whole thing. Every miscount is a gift.)

**Corpus:** 34,157 labeled decisions → 10,699 cycle-grouped examples
(BUY 8,246 / SHORT 8,430 / HOLD 17,467 / option 14), 200 cycles held
out. **Run:** 2,000 LoRA steps, run to completion (the training log
ends at step 2,000; an earlier version of this entry said it was
stopped at ~1,070, which the log does not support).

**What happened:** the fastest learning of any batch (val 2.078 →
0.822 by step 400, 0.816 at step 600), a rise to 0.952 at step 800,
then a genuine training COLLAPSE: validation exploded to 7.3 by step
1,000 and stayed there through step 2,000, train loss with it — the
constant 1e-5 learning rate that was fine for 600-step batches
destabilizes long runs. Every 100-step checkpoint is on disk; the
step-400 and step-600 checkpoints were examined.

**Exam (134 graded decisions, both surviving checkpoints):**

| | Base | Step-600 | Step-400 |
|---|---|---|---|
| Accuracy | **31.3%** | 27.6% | 26.1% |
| HOLD answers | 43/134 | 88/134 | 100/134 |
| Bearish hits | 11/50 | 0/50 | 2/50 |

**Verdict: not promotable — both checkpoints LOSE to the base.** And
with three batches on the board, the recurring failure mode is now
diagnosed, not guessed: hindsight relabeling turns every losing trade
into HOLD, so HOLD dominates the corpus (~51%) and imitation training
rewards blanket silence over discrimination — each batch has drifted
further into it, and batch 3's LR instability amplified the collapse
(its outputs even ramble after the JSON — weight degradation was
visible by step 600). The model keeps learning the corpus's loudest
lesson perfectly; the loudest lesson is "don't trade," and that alone
can't beat a base that actually discriminates.

**Batch 4 recipe (mandatory, from evidence):** rebalance the label
mix so HOLD can't dominate (weight or downsample toward
~⅓/⅓/⅓); learning-rate decay (cosine or step) for any run past ~600
steps; evaluate mid-run checkpoints as first-class candidates; report
frequency-matched-random (~33% here) alongside the base in every
exam so "beats base" can't hide behind class priors; pre-split the
few >8K-token prompts the truncation warning flagged.

---

## Forensic correction — 2026-09-20 · "The answers were barely in the loss"

Reading the batch 1–3 artifacts on the Mac before building batch 4
(adapter configs, training logs, and 400 sampled training examples per
batch run through the real tokenizer) found two defects in the training
driver that every batch above shares. They change how the three
verdicts should be read.

| | Batch 2 | Batch 3 |
|---|---|---|
| Prompt masking | off | off |
| Answer's share of the tokens the loss covered | 0.37% | 0.65% |
| Examples longer than the 8,192-token window | 30.8% | 39.8% |
| …of those, answer cut off entirely | 122 of 123 | 157 of 159 |

- **No prompt masking.** mlx-lm averages the loss over the whole
  sequence unless told otherwise. A ~7,000-token prompt with a
  ~20-token answer means more than 99% of every gradient step went to
  re-predicting the prompt.
- **The window was shorter than the prompts.** Doctrine item 4 below
  set the window to 8,192 while noting prompts run ~9–10K tokens;
  mlx-lm truncates the END of a long sequence, which is where the
  answer is. About a third of all training examples carried no answer.
  The truncation warning printed in every training log and was filed
  as a minor follow-up ("the few >8K-token prompts"). It was not minor
  and it was not few.

**What stands and what does not.** The exam scores are honest — the
exam generates from the full prompt and grades real answers — and no
adapter was ever promoted, hosted, or given a seat, so nothing in the
trading system was affected. What does not stand is the *explanation*:
"HOLD dominance in the corpus" was diagnosed from models that were
barely trained on their answers, so it is a hypothesis, not a finding.
Validation loss ("bottoms near step 400") was 25 unmasked examples and
measured prompt modelling, not decisions. Batch 3's exams used a
50-prompt limit (134 decisions, roughly ±8 points of noise), so its
27.6% vs 31.3% is not a distinguishable difference either way.

**Batch 4 is therefore the first batch that tests whether this
system's data improves the model.** Its recipe keeps the five changes
below and adds the two that matter most: prompt masking on, and
over-length examples split to fit (or dropped and counted) with a
refuse-to-train guard, because masking plus a truncated answer is a
divide-by-zero in the trainer's loss. Details and build status:
`28_FINETUNE_BATCH4_BUILD_SPEC.md` §0.

Doctrine, amended: **6. Prompt masking is always on, and no example
longer than the training window is ever written** — the trainer
refuses to start otherwise. Item 4's window stays at 8,192; what
changed is that examples are made to fit it instead of being cut.

---

## Batch 4 — 2026-09-20/21 · "The plumbing works; the lesson doesn't"

The first batch trained on its answers, and the first examined on an
exam that could not flatter it. **Verdict: not promotable — no
checkpoint clears the bar.** What it taught is more specific than the
score.

**Corpus** (`~/Quantops-finetune/data/20260920_170932`, journals
snapshotted 2026-09-20): 56,552 labeled decisions in 16,705 cycles —
Experiment 1's archive (34,157) plus Experiment 2 (22,395; its other
~4,700 labelable rows belong to cycles whose AI call FAILED, which are
not decisions — see CHANGELOG 2026-09-20). Split in time: training ends
2026-09-03, validation starts 09-08, the exam is 200 cycles sampled
evenly across 09-09 / 09-10 / 09-11; 1,715 training cycles purged
because their outcomes resolved inside the later blocks. Unlabeled
candidates pruned from train/val prompts (18,844 blocks). Length pass
with the real tokenizer: 4,191 training cycles split, **0 dropped, 0
labels lost, longest example 8,192 tokens**. Rebalance: empty-target
share 27.4% → 10.0%; BUY:SHORT examples 7,693 : 8,924 (inside 1.25×,
untouched). **15,976 train / 215 val / 426 exam examples.** Label
origins in training: missed moves 18,047, flat HOLDs 13,384, losing
entries 4,653, winning entries 4,307 — 80.8% of directional labels are
hindsight "missed moves".

**Run:** prompt masking ON (the trainer's own record and mlx-lm's
loader both confirmed the loss covered exactly the answer), 60-step
warmup to 1e-5 then cosine, batch size 1, 8,192 window, ~26 s/step.
Planned 1,200 steps; **macOS killed the job at step ~630** (`[METAL]
Command buffer execution failed: Impacting Interactivity` — the OS
reclaiming the GPU, not a recipe fault). Checkpoints 100–600 survived.
Validation loss, answers only, 100 examples: **3.616 → 0.595 (100) →
0.605 → 0.625 → 0.748 → 0.548 (500) → 0.673 (600)** — everything it
was going to learn about the loss it learned in the first 100 steps;
after that, noise. Not resumed: more steps of this recipe were not the
missing ingredient, and the exam below agrees.

**Exam** — 426 prompts, 613 graded decisions, **160 distinct
stock-days** (twelve replicate profiles judge the same stock on the
same day, so that, not 613, is the real sample). Labels: HOLD 59.1%,
bearish 31.2%, bullish 9.6%. Guessing baselines: always-HOLD **59.1%**,
frequency-matched 45.5% (5th–95th percentile 42.4–48.6%).

| | Accuracy | Bullish | Bearish | HOLD | What it answered | vs base, by stock-day |
|---|---|---|---|---|---|---|
| Untrained base | 40.8% | 32/59 | 124/191 | 94/362 | bearish 273 · bullish 181 · hold 138 · option 20 | — |
| Step 100 | **14.5%** | 57/59 | 14/191 | 18/362 | **bullish 569** · hold 22 · bearish 22 | 16 : 69 (worse, p<0.001) |
| Step 200 | **46.8%** | 1/59 | 97/191 | 189/362 | hold 307 · bearish 301 · bullish 5 | 70 : 42 (p=0.010) |
| Step 500 *(lowest validation loss — the pick made without looking at the exam)* | 36.0% | 31/59 | 38/191 | 152/362 | hold 265 · bullish 257 · bearish 91 | 47 : 51 (p=0.76) |
| Step 600 | 43.7% | **0/59** | 109/191 | 159/362 | bearish 351 · hold 262 · **bullish 0** | 59 : 44 (p=0.17) |

Every checkpoint fails the promotion bar: none beats the guessing
band's 95th percentile (48.6%), none comes near always-HOLD (59.1%),
and steps 200, 500 and 600 are each worse than the base on BOTH
directional classes. Step 200 does beat the base overall, even counted
by stock-day — and the bar is right to refuse it: it got there by
almost never saying BUY in a falling week.

**What the raw answers show** (every generation is kept in the report;
ten were read for each of the best and the last checkpoint):

- **The adapter learned to emit exactly ONE bare trade.** Median answer
  46 characters — `{"trades":[{"symbol":"X","action":"SHORT"}]}` —
  against the base's 1,420. This is batch 1's one-pick convention back
  again, and it mirrors the targets: 8,280 of 15,976 contain exactly one
  trade, and 81% of all target trades are the bare symbol-and-action
  form a "missed move" label produces.
- **The action is a global prior, and it swings.** BUY on 569 of 613 at
  step 100; BUY on 5 at step 200; BUY on 257 at step 500; BUY on **0** at
  step 600. One example per step, ~20 graded tokens each: every update
  chases the last few labels it saw.
- **The symbol is loosely chosen.** With only `BAC` labeled it answered
  `META`; with only `GS` labeled, `AMZN`. It is picking *a* candidate,
  not judging *the* candidates.
- The one thing that looks like skill is not: step 200 scores 47/77 on
  "entries that lost" (base: 2/77) only because it leaves most
  candidates out, and an omission is a HOLD.
- Format is perfect (0 unparseable in all four; base 1), as in batch 2.

**Verdict.** The training now does what it was built to do — the model
learns from its answers, fast — and what it learns is the SHAPE and the
BASE RATE of the targets, not which candidates deserve a trade. Two
causes are visible in the evidence rather than guessed at:

1. **Step noise.** Batch size 1 with ~20 graded tokens per step. The
   answer mix lurching from all-BUY to no-BUY between checkpoints is
   what that looks like.
2. **The lesson is mostly unlearnable.** 80.8% of directional labels
   are "missed moves": the AI said HOLD and the stock then moved more
   than 5%. Nothing in the prompt may distinguish those from the flat
   ones — the base can't either — so the only thing to learn from them
   is how often they happen. The labels that carry a learnable question
   — *of the entries this system actually made, which won?* (4,307 won,
   4,653 lost) — are 1 in 5 and are drowned out.

**Batch 5 changes — both required before any retrain, each answering
one cause above:** (1) **gradient accumulation** (16–32 examples per
update) so an update reflects a label mix, not the last example — needs
a driver flag; (2) the **missed-move cap** (built, default off —
`--missed-move-cap`), set low enough that own-entry labels dominate the
directional signal. And the exam must report **own-entry
discrimination** as its own line — accuracy over the `kept_win` +
`lost_entry` decisions only (187 of this exam's 613) — because that,
not predicting which stock jumps 5%, is the question an owned model
could plausibly win. No retrain on this recipe: it would reproduce this
result.

---

## Where things stand — 2026-09-21 (batch 4 examined: not promotable)

**The owned model is not in use.** It has never held a seat, made a
decision, or been hosted; the trading system's live learning (the
self-tuner, the meta-model, specialist calibration, veto feedback,
the Experiment-2 arms) is a separate set of mechanisms and is not
affected by anything in this log. The app says the same thing: the
Learning page carries an "Our own model" panel driven by
`finetune/status.json`, which is updated with every verdict here.

**Four batches, none promotable.** Batches 1–3 were not a fair test
(the forensic correction above); batch 4 was, and it failed the bar for
reasons that are now specific: the adapter learns the targets' shape
and base rate — one bare trade, a swinging action prior — not which
candidates deserve a trade. Nothing is hosted, no money is spent.

**The batch-4 recipe stands** (2026-09-20, `finetune/` +
`tests/test_finetune_batch4_recipe_2026_09_20.py`): prompt masking
always on; over-length examples split along the candidate table or
dropped and counted, never truncated; a refuse-to-train guard;
internal bookkeeping keys stripped from targets; train-split
rebalancing with label origins; warmup-plus-cosine learning rate;
checkpoint sweep against one set of base answers; guessing baselines,
a paired significance test and an explicit promotion bar. What each
part does and why is in `28_FINETUNE_BATCH4_BUILD_SPEC.md` §4; its §0
tracks what has been run.

**The data has arrived.** Running the builder's own `hindsight_label()`
read-only over the live Experiment-2 journals (profiles 229–240,
2026-08-24 → 2026-09-19):

| | Batch 3 corpus (Exp 1) | New in Experiment 2 |
|---|---|---|
| Labeled decisions | 34,157 | **27,054** (+79%) |
| Cycle-grouped examples | 10,699 | **7,008** |
| Label mix | BUY 24% / SHORT 25% / HOLD 51% | BUY 16.4% / SHORT 23.6% / **HOLD 60.0%** |
| Teachers | Experiment-1 models | four arms, near-even: `gpt-4.1-nano` 6,150 · `gpt-5.6-luna` 6,069 · `gemini-3.5-flash-lite` 7,679 · `gemini-3.7-flash` 7,156 |

Another 11,806 predictions were still unresolved, and 11,802 resolved
rows fell in the discarded 2–5% gray zone. Two things follow. The new
data is a second market regime and four different decision-makers —
the variety batch 2's verdict said was missing. And it is more
HOLD-skewed than batch 3's corpus, which is why the train split is
rebalanced — though whether HOLD skew was ever the cause of the poor
scores is exactly what batch 4 tests (see the forensic correction
above).

### When to train the next batch

1. **Batch 5: only after its two recipe changes exist** — gradient
   accumulation in the driver, and the missed-move cap turned on (it is
   built, default off) — plus the own-entry discrimination line in the
   exam. See batch 4's verdict. The Mac runbook is
   `docs/28_FINETUNE_BATCH4_BUILD_SPEC.md` §6; the exam is resumable
   and the base's answers for an unchanged exam are already on disk.
2. **After batch 4 — evidence-gated, not calendar-gated.** Retrain when
   *either* the labeled corpus has grown by ≥10,000 decisions since the
   last batch (≈ every 10–14 days at Experiment 2's ≈7,000/week)
   *or* the previous batch's verdict named a specific recipe change
   that has since been built. Never retrain on the same recipe and
   near-identical data — a 5-hour run that cannot differ from the last
   one teaches nothing. (The weekly Sunday cadence in doc 20 belonged
   to the hosted-vendor design, where an increment cost minutes and
   cents; it does not transfer to local LoRA runs.)
3. **The promotion bar does not move:** a clear win over both the
   untrained base and frequency-matched random on the held-out exam
   before any hosting spend or shadow seat.

