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
| Corpus source | `backups/predictions_archive` + live journals, cycle-joined | 46,583 resolved predictions from Experiment 1, 100.0% joinable to their full prompts; grows daily — all four Experiment-2 arms (primaries and shadows) feed it |

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
out. **Run:** 2,000 LoRA steps planned; stopped at ~1,070.

**What happened:** the fastest learning of any batch (val 2.078 →
0.822 by step 400 — best-ever), a plateau, then a genuine training
COLLAPSE: validation exploded to 7.3 by step 1,000 and train loss
followed — the constant 1e-5 learning rate that was fine for
600-step batches destabilizes long runs. Stopped early; every
100-step checkpoint was on disk, so nothing was lost but time.

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
