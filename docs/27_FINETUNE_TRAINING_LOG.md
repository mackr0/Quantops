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

## Batch 3 — queued

**Trigger:** corpus roughly doubles (~3–4 weeks; all four
Experiment-2 arms now feed it, so it grows ~4× faster than the
Experiment-1 era). **Planned levers:** label-balance weighting so
caution isn't over-rewarded; 2–3 epochs over the full corpus;
evaluate the mid-run checkpoint alongside the final. **The bar is
unchanged:** beat the base clearly, then the hosting question — and a
seat at the table — reopens.
