# 28 — Fine-Tune Batch 4: Build Spec and Handoff

**Audience:** the engineer (or Claude session) who builds the batch-4 recipe and the operator who runs the training on the Mac.
**Purpose:** everything needed to do that work without re-deriving it. Self-contained; read this first, then the two companions.
**Written:** 2026-09-19. **Status:** NOT STARTED — nothing below exists in `finetune/` yet (last code change there: 2026-08-27).

Companions: `docs/27_FINETUNE_TRAINING_LOG.md` (what batches 1–3 did and why they failed — the evidence this spec answers) and `docs/20_FINETUNE_PHASE_4B1_INCREMENTAL.md` (original design; only its §16.1 local-LoRA path, §5 data rules, §8 evaluation philosophy and §18 portability contract are live — its weekly hosted-vendor body is not what runs, and its §16.1 "iteration loop" cites a build script that was never written; the real entry point is the `build-corpus` subcommand of `finetune/local_train.py`).

---

## 1. The problem in one paragraph

We fine-tune a small open model (Qwen2.5-7B-Instruct, LoRA, Apple MLX, on the operator's M2 Max — never on the droplet, $0) on this system's own decisions, relabeled in hindsight: a winning entry keeps its action, a losing entry becomes HOLD, a HOLD that then moved >5% becomes the missed direction, 2–5% moves are discarded. Three batches have been trained and none beat its own untrained base on the held-out exam (batch 2: 38.6% vs 37.3%, a tie; batch 3: 27.6% vs 31.3%, a loss, after a training collapse past step ~1,000). The diagnosed cause is **HOLD dominance**: the corpus's loudest lesson is "don't trade", the adapter learns it as a blanket prior (batch 3 answered HOLD on 88–100 of 134 decisions and hit 0–2 of 50 bearish calls), and blanket silence cannot beat a base that discriminates. Batch 4 is the same pipeline with five mandatory changes.

## 2. Where everything lives

| Thing | Location |
|---|---|
| Labeling rules, look-ahead guard, corpus build, train/val/eval split | `finetune/dataset_builder.py` — `hindsight_label()`, `option_hindsight_label()`, `assert_no_lookahead()`, `build_cycle_example()`, `build_dataset()` |
| Training + exam driver (three subcommands: build-corpus / train / eval) | `finetune/local_train.py` — `cmd_build_corpus()`, `build_train_command()`, `cmd_train()`, `parse_decision()`, `direction_bucket()`, `score_examples()`, `cmd_eval()` |
| Model registry tables (unused until something is promotable) | `finetune/model_registry.py` |
| Portability dry-run | `finetune/dryrun_portability.py` |
| Existing tests (extend these; match their style) | `tests/test_finetune_dataset_builder.py`, `tests/test_finetune_local_train_2026_08_26.py`, `tests/test_finetune_cycle_join_2026_08_26.py`, `tests/test_finetune_no_lookahead_bias.py` |
| Corpus, on the droplet | `backups/predictions_archive/` (Experiment 1: 22 dumps, pids 207–219) + the live journals `quantopsai_profile_229.db` … `quantopsai_profile_240.db` (Experiment 2) |
| Corpus, on the Mac | `~/Quantops-finetune/corpus/predictions_archive/` and `~/Quantops-finetune/corpus/profile_dbs/` (the driver's default workdir; `build-corpus` refuses to run without the archive snapshot) |
| Batch history and verdicts | `docs/27_FINETUNE_TRAINING_LOG.md` — add the batch-4 entry there when it has run |

House rules apply (`DROPLET_DEV.md`): work on a branch, zero-fail zero-skip full suite before merge, dated CHANGELOG entry, nothing deferred. The `finetune/` code is pure Python and fully testable on the droplet; only `train` and `eval` need the Mac (they import `mlx_lm` lazily, so the suite never needs it).

## 3. What the data looks like today (measured 2026-09-19 — re-measure before building)

Pooled corpus ≈ 61,000 labeled decisions: batch 3's 34,157 (Experiment 1) + 27,054 new from Experiment 2's first four weeks (7,008 cycle-grouped examples), across four teacher models in near-even shares. The Experiment-2 slice, by where each label came from:

| Label | Count | Origin |
|---|---|---|
| HOLD | 13,358 | the AI said HOLD and the stock stayed flat (<2%) — *easy negatives* |
| HOLD | 2,872 | the AI entered and lost (1,593 long, 1,279 short) — *the valuable "this looked good and wasn't" lesson* |
| SHORT | 5,095 | the AI said HOLD and the stock fell >5% — a missed move |
| BUY | 3,454 | the AI said HOLD and the stock rose >5% — a missed move |
| SHORT | 1,303 | winning short entry |
| BUY | 972 | winning long entry |

Two facts in that table drive the design:

1. **82% of HOLD labels are easy negatives**, and **79% of BUY/SHORT labels are "missed moves"** — hindsight on >5% movers the AI passed on. Only ~5,100 labels (19%) come from trades the system actually took. The corpus mostly teaches "what happened next", not "which of my entries were good".
2. **The label mix is not what the loss sees.** Examples are cycle-grouped (`build_cycle_example()`): the training target is `{"trades": [...]}` listing only the non-HOLD candidates; HOLD is expressed by *omission*. So a HOLD label contributes no target tokens — it is an absence. At cycle level: of 7,008 cycles, **1,467 (20.9%) have an empty target** (every labeled candidate is HOLD) and 5,541 carry at least one action (3,053 with a BUY, 4,001 with a SHORT, 1,513 with both); the median cycle has 4 labeled candidates (max 11). Even inside action-bearing cycles, HOLD is still 52.6% of labels. **Dropping empty-target cycles alone does not reach a ⅓/⅓/⅓ label mix — and it does not need to:** the quantity to control is the *shape of the target* (how often it is empty, how many trades it lists), not the label census.

## 4. The five changes (all mandatory; each answers a documented failure)

### 4.1 Rebalance the training split — `finetune/dataset_builder.py`

*Failure answered:* every batch drifted further into blanket HOLD.

- Add a rebalancing stage inside `build_dataset()`, applied **after** the eval holdout and the val split are taken and **to the train split only**. Eval and val must keep the natural distribution — they measure the real task, and a rebalanced exam would flatter the model.
- Controls (keyword arguments with these defaults, surfaced as `build-corpus` flags in `finetune/local_train.py`):
  - *max empty-target fraction* — cap cycles whose target is `{"trades": []}` at **10%** of the train split (natural: ~21%). Keep some: "nothing here is worth trading" is a real answer and the model must still be able to give it.
  - *direction balance* — after the cap, if cycles containing a BUY and cycles containing a SHORT differ by more than 1.25×, downsample the majority side's single-direction cycles toward parity (natural: 3,053 vs 4,001). Never split a cycle or edit its target to achieve this — the target must remain the true corrected action set for that prompt.
  - *seed* — sampling is deterministic from `build_dataset()`'s existing `seed`.
- When choosing which empty-target cycles to keep, **prefer those containing at least one losing-entry HOLD** over those made only of flat easy negatives: they carry the discrimination lesson (§3, fact 1). This requires the label's origin, so have the labeling path return it — extend the tuple produced by the internal label-and-return helper with an origin tag (`kept_win` / `lost_entry` / `flat_hold` / `missed_move`) and carry it into each example's `_meta` (it never reaches the training file; `_meta` is stripped on write).
- The manifest must report, before and after rebalancing: example count, empty-target fraction, mean trades per target, label distribution, and label-origin distribution. A rebalance that cannot be audited from the manifest is not done.
- Do **not** implement per-example loss weights: HOLD is an omission, there are no HOLD tokens to weight.
- *Open question to settle with data, not opinion:* "missed move" labels are 79% of the directional signal and are the noisiest (was a +6% move knowable from that prompt?). Add a build flag to cap the missed-move share of directional labels, **default off** for batch 4 so the batch changes one thing at a time; record its distribution in the manifest so batch 5 can test it.

### 4.2 Learning-rate schedule — `finetune/local_train.py`

*Failure answered:* batch 3's constant 1e-5 destabilized past ~600 steps; validation loss went from 0.82 to 7.3.

- `build_train_command()` currently passes no learning-rate arguments at all (mlx-lm's default constant rate). Add cosine decay with a short warmup: peak **1e-5**, ~5% warmup steps, decaying to ~1e-7 at the final iteration.
- mlx-lm takes schedules through its YAML config file (`--config`), not a plain CLI flag, in the versions we have used: a `lr_schedule` block (`name: cosine_decay`, `warmup`, `arguments: [peak, decay_steps, end]`). **Verify against the installed version on the Mac before coding** — run the `lora` subcommand's `--help` and check the package's example LoRA config. The cleanest implementation writes a config file into the adapter directory and passes `--config`, keeping `build_train_command()` pure so the existing command-pinning test pattern still works (pin the generated config's contents too).
- While in that config, check whether the installed mlx-lm supports **prompt masking** (training loss on the assistant completion only). Our prompts are ~9–10K tokens and the target is a few dozen; if the loss currently averages over prompt tokens, nearly all of the gradient is spent re-predicting the prompt. If supported, turn it on and say so in the batch log — it may matter more than anything else here. If not supported, record that in the log.

### 4.3 Mid-run checkpoints are first-class candidates — `finetune/local_train.py`

*Failure answered:* validation loss bottomed near step 400 in all three batches; evaluating only the final adapter threw away the best model.

- Train: pass a save interval (every 100 steps) and a validation interval so checkpoints and their val losses exist. mlx-lm writes numbered adapter files beside `adapters.safetensors` in the adapter directory.
- Eval: `cmd_eval()` takes one `--adapter` directory today. Add a sweep mode that evaluates a list of checkpoints (default: the three with the lowest validation loss, plus the final one) against the same prompts, generating the **base answers once** and reusing them. Each checkpoint needs its numbered file staged as `adapters.safetensors` next to the adapter config in its own temp directory, because that is what the mlx-lm loader reads.
- The report names the winner and keeps every checkpoint's scores and generations (doctrine item 2 in doc 27: an unexplained score is forensically worthless).

### 4.4 Frequency-matched-random baseline in the exam — `finetune/local_train.py`

*Failure answered:* "beats the base" can hide behind class priors (batch 3's exam: ~33% is what label-frequency guessing scores).

- In `score_examples()`' caller, add a third scored row beside `base` and `adapter`: a guesser that draws each answer from the **eval set's own label frequencies**, averaged over 1,000 seeded draws (report mean and the 5th–95th percentile band), plus the two trivial baselines "always HOLD" and "always the majority class".
- **The promotion bar** (unchanged in spirit, now explicit): the winning checkpoint must beat the untrained base **and** the random band's 95th percentile on overall accuracy, **and** must not be worse than the base on both directional classes (a model that wins overall by answering HOLD is batch 2 again). Report per-class accuracy and the answer mix every time.

### 4.5 Over-length prompts — `finetune/dataset_builder.py`

*Failure answered:* a few prompts exceed the 8,192-token training window; mlx-lm truncates them silently, cutting off part of the candidate table, and the model is then trained (and graded) on candidates it never saw.

- At build time, estimate each example's token length. No tokenizer exists on the droplet, so use a conservative character-based estimate (calibrate the chars-per-token ratio once on the Mac against the real tokenizer and record it in the code comment) with a safety margin.
- For an over-length example, **split the candidate table** into two or more prompts that each fit, each keeping the full shared context, with the target restricted to the candidates present in that split. If the prompt's structure cannot be split safely (the candidate block cannot be located), **drop the example and count it** — never emit a truncated one. Apply the same rule to eval examples.
- Manifest: number split, number dropped, max estimated length after the pass.

## 5. Tests to add (suite must stay zero-fail, zero-skip)

- Rebalance: train-only (eval/val distributions unchanged); empty-target cap honored; deterministic under a fixed seed; no cycle's target is altered; losing-entry empties preferred over flat-only empties; manifest carries before/after numbers; look-ahead guard still asserted on every row that survives.
- Label origin: each of the four origins produced from the right (signal, outcome, return) inputs; origin never appears in the written training files.
- LR schedule: the generated config contains the cosine schedule with the expected peak/warmup/end; `build_train_command()` stays pure.
- Checkpoint sweep: checkpoint discovery and selection by validation loss from a fabricated adapter directory; base answers generated once (mock the generator and count calls).
- Baselines: frequency-matched expectation matches the closed form on a toy distribution; always-HOLD and majority baselines; promotion-bar function returns False for a HOLD-collapsed answer set that wins on overall accuracy.
- Over-length: a synthetic long prompt is split into fitting prompts whose targets partition the original; an unsplittable one is dropped and counted; nothing over the limit is ever written.

## 6. Running batch 4 (operator, on the Mac)

```bash
# 1. Snapshot the corpus from the droplet (read-only; safe any time)
mkdir -p ~/Quantops-finetune/corpus/profile_dbs
rsync -az root@67.205.155.63:/opt/quantopsai/backups/predictions_archive/ \
      ~/Quantops-finetune/corpus/predictions_archive/
rsync -az 'root@67.205.155.63:/opt/quantopsai/quantopsai_profile_2[23][0-9].db' \
      'root@67.205.155.63:/opt/quantopsai/quantopsai_profile_240.db' \
      ~/Quantops-finetune/corpus/profile_dbs/

# 2. From an up-to-date checkout of the repo on the Mac:
python -m finetune.local_train build-corpus            # prints the manifest; read it
python -m finetune.local_train train --data <corpus dir> --iters 1200
python -m finetune.local_train eval  --data <corpus dir> --adapter <adapter dir>   # + the new sweep flag
```

Keep the live-DB file names intact — the builder derives each profile's dedup namespace from `quantopsai_profile_<id>.db`, and a renamed file would let one profile's rows swallow another's (the batch-3 dedup bug). Copy only profiles 229–240; older profile DBs on the droplet are stale. Expect ~5–10 hours of training; gradient checkpointing stays on; 1,200 steps is a starting point now that the schedule decays — the checkpoint sweep, not the step count, picks the model.

**Before trusting any number:** open the manifest and confirm the empty-target fraction and label-origin mix are what §4.1 says they should be; open a handful of training examples and read them; after eval, read ten raw generations from the winning checkpoint. Batches 1–3 each had a defect that only reading the artifacts revealed.

## 7. Definition of done

1. The five changes are merged with their tests; full suite green; CHANGELOG entry.
2. Batch 4 has been trained and examined on the Mac, and `docs/27_FINETUNE_TRAINING_LOG.md` has its entry — corpus and rebalance numbers, loss curve, the checkpoint sweep table against base / random band / always-HOLD, the answer mix, and a verdict.
3. If the bar in §4.4 is cleared: stop and bring the result to the operator — hosting, the shadow seat and any spend are their decision (`docs/25_MODEL_SELECTION_AND_LEARNING_PLAN.md` step 4.5). If it is not cleared: the log entry names the specific next change (the missed-move cap from §4.1 is the first candidate), and that change is built before batch 5 — never retrain on the same recipe and near-identical data.
