# 28 — Fine-Tune Batch 4: Build Spec and Handoff

**Audience:** the engineer (or Claude session) who builds the batch-4 recipe and the operator who runs the training on the Mac.
**Purpose:** everything needed to do that work without re-deriving it. Self-contained; read this first, then the two companions.
**Written:** 2026-09-19; corrected and built 2026-09-20. **Status:** the recipe is BUILT and tested (§0 tracks every item); batch 4 itself is trained and examined on the Mac — see §0 for where that stands.

Companions: `docs/27_FINETUNE_TRAINING_LOG.md` (what batches 1–3 did and why they failed — the evidence this spec answers) and `docs/20_FINETUNE_PHASE_4B1_INCREMENTAL.md` (original design; only its §16.1 local-LoRA path, §5 data rules, §8 evaluation philosophy and §18 portability contract are live — its weekly hosted-vendor body is not what runs, and its §16.1 "iteration loop" cites a build script that was never written; the real entry point is the `build-corpus` subcommand of `finetune/local_train.py`).

---

## 0. Build plan and progress

Work happens on the Mac (it has the tokenizer, mlx-lm, the adapters and SSH to the droplet), on branch `feat/finetune-batch4-recipe`. Items are checked off here as they are completed and verified — an unchecked item is not done.

- [x] **P1 — Forensics recorded.** The measured facts in §0.1 are written into this doc and doc 27 before any code changes. *(2026-09-20)*
- [x] **P2 — Over-length handling** (§4.5): candidate-table split, drop-and-count when unsplittable, applied to train, val and eval; nothing over the window is ever written. *(Verified on all 9,450 batch-3 prompts — 100% byte-exact round trip — and on the batch-4 corpus: 0 dropped, 0 labels lost, longest example 8,192 tokens.)*
- [x] **P3 — Target hygiene**: internal underscore-prefixed keys (`_ledger_rar`, …) never reach a training target. *(Verified: no underscore key in any of the batch-4 corpus's target trades.)*
- [x] **P4 — Label origin** carried in `_meta` (`kept_win` / `lost_entry` / `flat_hold` / `missed_move`), never written to training files.
- [x] **P5 — Train-split rebalance** (§4.1): empty-target cap, direction balance, losing-entry empties preferred, missed-move cap flag (default off), before/after manifest.
- [x] **P6 — Training config** (§4.2): cosine schedule with warmup, **prompt masking on**, save and validation intervals, larger validation sample; default base is the 4-bit model every batch has used. *(Masking verified with mlx-lm's own dataset loader on 300 random batch-4 examples: the span the loss scores is exactly the answer plus the end-of-turn token in all 300.)*
- [x] **P7 — Refuse-to-train guard**: `train` tokenizes the corpus with the real tokenizer and refuses to start if any example exceeds the window or has no answer tokens inside it.
- [x] **P8 — Checkpoint sweep** (§4.3): discovery, selection by validation loss, base answers generated once, every checkpoint's scores and generations kept.
- [x] **P9 — Baselines and promotion bar** (§4.4): frequency-matched random band, always-HOLD, majority class, paired test per distinct stock-and-day, explicit promotion function.
- [x] **P9a — Found reading the first batch-4 manifests and examples, before training** (§4.8–4.11): snapshot guard; time-ordered split with a purge (the exam would have leaked through overlapping outcome windows); exam sampled across three trading days (it was a single day); failed AI calls excluded on purpose; unlabeled candidates pruned from train/val prompts (they were being taught as HOLD by omission).
- [x] **P10 — Tests** (§5) for every item above; full suite zero-fail, zero-skip on the final code. *(2026-09-20, Mac: 7,113 passed / 0 failed / 0 skipped / 0 warnings.)*
- [x] **P11 — Docs and CHANGELOG** describe the code as built (docs 02, 17, 20, 25, 27, 28, README index, OPEN_ITEMS, the Learning page's calculation register). *(The batch-4 result itself is P14.)*
- [x] **P12 — Corpus snapshot** pulled from the droplet (archive + journals 229–240 by SQLite online backup, 2026-09-20); corpus re-measured; manifests and examples read. *(Final corpus `~/Quantops-finetune/data/20260920_170932`: 56,552 labeled decisions in 16,705 cycles → 15,976 train / 215 val / 426 exam examples; exam = 200 cycles over 09-09/10/11, 613 graded decisions, 160 distinct stock-days.)*
- [ ] **P13 — Batch 4 trained** on the Mac. *(Launched 2026-09-20, 1,200 steps; adapter `~/Quantops-finetune/adapters/20260920_171835`, log `~/Quantops-finetune/train_batch4.log`. The trainer's own run record shows `mask_prompt: True` and the schedule read as numbers; the length guard found 0 of 15,976 train and 0 of 215 val examples over the window. Measured: ~26 s per step and ~18 min per 100-example validation pass, so ~12–13 hours in all. After 20 steps it had trained on 568 tokens — answers only — where batch 3 trained on ~7,000 prompt tokens per step.)*
- [ ] **P14 — Batch 4 examined**: sweep run on the full holdout, ten raw generations read, doc 27 entry and verdict written, `finetune/status.json` updated.
- [x] **P16 — Merged, pushed, deployed**; the Learning-page panel verified on prod. *(2026-09-20: prod at the merge commit, both services active; `/learning` rendered through the deployed app returns 200 with the panel and the four-arm scoreboard.)*
- [x] **P15 — The Learning page tells the owned model's real status.** *(Built and tested; live on prod after P16.)* `/learning` today covers only the four rented-model arms and says nothing about the owned model, so "trained three times, never good enough, not in use" was invisible in the app. A status panel, driven by a status file updated with every batch verdict, states it in plain English; Flask-client test pins it.

### 0.1 What the batch 1–3 artifacts actually show (measured on the Mac, 2026-09-20)

Measured with the real Qwen tokenizer over 400 sampled training examples per batch, and read from each batch's `adapter_config.json` and training log in `~/Quantops-finetune/`:

| | Batch 2 | Batch 3 |
|---|---|---|
| Prompt masking | **off** | **off** |
| Answer's share of the tokens the loss was computed over | **0.37%** | **0.65%** |
| Example length, tokens (p10 / p50 / p90 / max) | 4,547 / 6,561 / 12,417 / 16,176 | 4,652 / 7,516 / 11,444 / 18,024 |
| Examples longer than the 8,192-token window | **30.8%** | **39.8%** |
| …of those, answer cut off **entirely** | 122 of 123 | 157 of 159 |
| Answer length, tokens (p50 / p90) | 14 / 101 | 22 / 140 |

Batch 1 ran with the same configuration (masking off, 8,192 window, constant 1e-5).

What this means, stated plainly:

1. **No batch has yet been trained mainly on its answers.** mlx-lm averages the loss over every token unless `--mask-prompt` is passed; it never was. More than 99% of each gradient step went to re-predicting prompt text.
2. **Roughly a third of training examples contained no answer at all.** mlx-lm truncates the *end* of an over-length sequence, and the answer is the last few dozen tokens. §4.5 below called this "a few prompts"; it was 31–40% of the corpus.
3. **The adapters still learned from what got through** — batch 2 went from 9 unparseable answers to 0 and lifted HOLD accuracy from 22/55 to 41/55 — so the HOLD-collapse is a real output behaviour. But its diagnosed cause ("HOLD dominance in the label mix") was inferred from models trained under defects 1 and 2, so it is a hypothesis batch 4 tests, not an established fact.
4. **Validation loss measured prompt modelling.** It was computed over 25 unmasked examples, so "validation bottoms near step 400" says nothing about decision quality. Checkpoint selection by validation loss only becomes meaningful with masking on.
5. **Masking and truncation interact fatally.** With masking on, a fully-truncated example has zero loss tokens and mlx-lm's loss divides by zero (NaN at batch size 1). Over-length handling is therefore a hard prerequisite for masking, enforced by a refuse-to-train guard (P7).
6. Batch 3 ran all 2,000 steps (validation 0.822 at 400, 0.816 at 600, 0.952 at 800, 7.3 from 1,000 on); it was not stopped at ~1,070. Its exams used a 50-prompt limit (134 graded decisions, roughly ±8 points of sampling noise), so 27.6% vs 31.3% is not a distinguishable difference.
7. 18% of target trade dicts carried internal bookkeeping keys (`_ledger_rar`, `_ledger_best_rar`, `_ledger_best_expr`, `_ledger_is_override`) copied from the stored response — fields the production AI never emits.
8. Tokenizer calibration for §4.5: characters per token on real examples runs min 2.68, 5th percentile 2.76, median 3.02.
9. Installed trainer: mlx-lm 0.31.3. `--mask-prompt` is a plain flag (config key `mask_prompt`); a learning-rate schedule is config-file only — `lr_schedule: {name: cosine_decay, warmup: N, warmup_init: 0.0, arguments: [peak, decay_steps, end]}`, where the warmup and the decay are joined end to end, so `decay_steps` is the iteration count minus the warmup.

## 1. The problem in one paragraph

We fine-tune a small open model (Qwen2.5-7B-Instruct, LoRA, Apple MLX, on the operator's M2 Max — never on the droplet, $0) on this system's own decisions, relabeled in hindsight: a winning entry keeps its action, a losing entry becomes HOLD, a HOLD that then moved >5% becomes the missed direction, 2–5% moves are discarded. Three batches have been trained and none beat its own untrained base on the held-out exam (batch 2: 38.6% vs 37.3%, a tie; batch 3: 27.6% vs 31.3%, a loss, after a training collapse past step ~1,000). Two causes are on the table. The first is established (§0.1): **the training driver barely trained on the answers** — prompt masking was never on, so the answer was under 1% of the loss, and about a third of examples were longer than the training window, so the trainer cut their answers off entirely. The second is a hypothesis drawn from those flawed runs: **HOLD dominance** — the corpus's loudest lesson is "don't trade", the adapter learns it as a blanket prior (batch 3 answered HOLD on 88–100 of 134 decisions and hit 0–2 of 50 bearish calls), and blanket silence cannot beat a base that discriminates. Batch 4 is the same pipeline with both addressed: the two driver defects fixed (§4.2, §4.5, §4.7) and the five recipe changes below.

## 2. Where everything lives

| Thing | Location |
|---|---|
| Labeling rules, label origin, look-ahead guard, corpus build, train/val/eval split | `finetune/dataset_builder.py` — `hindsight_label()`, `option_hindsight_label()`, `label_origin()`, `assert_no_lookahead()`, `build_cycle_example()`, `build_dataset()` |
| Length control (candidate-table split, drop-and-count) | `finetune/dataset_builder.py` — `CharEstimateCounter`, `fit_example()`, `fit_split()` |
| Train-split rebalance and split statistics | `finetune/dataset_builder.py` — `rebalance_train()`, `split_stats()` |
| Training + exam driver (three subcommands: build-corpus / train / eval) | `finetune/local_train.py` — `cmd_build_corpus()`, `check_profile_snapshots()`, `TokenizerCounter`, `build_train_command()`, `build_train_config()`, `check_corpus_lengths()`, `cmd_train()`, `parse_decision()`, `score_examples()`, `baseline_scores()`, `paired_comparison()`, `promotion_bar()`, `select_checkpoints()`, `cmd_eval()` |
| The owned model's standing, as the app shows it | `finetune/status.json` (updated with every batch verdict), read by `finetune/status.py` and rendered on the Learning page |
| Model registry tables (unused until something is promotable) | `finetune/model_registry.py` |
| Portability dry-run | `finetune/dryrun_portability.py` |
| Tests | `tests/test_finetune_batch4_recipe_2026_09_20.py` (this recipe), `tests/test_learning_own_model_panel_2026_09_20.py` (the app panel), plus `tests/test_finetune_dataset_builder.py`, `tests/test_finetune_local_train_2026_08_26.py`, `tests/test_finetune_cycle_join_2026_08_26.py`, `tests/test_finetune_no_lookahead_bias.py` |
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

## 4. The recipe as built (every part answers a documented failure)

§4.1–4.5 are the five changes this spec was written for; §4.6–4.11 were added on 2026-09-20 — §4.6–4.8 from the §0.1 forensics, §4.9–4.11 from reading the first batch-4 corpus manifests and examples before training on them. All are in the code and pinned by tests.

### 4.1 Rebalance the training split — `rebalance_train()` in `finetune/dataset_builder.py`

*Failure answered:* every batch drifted further into blanket HOLD.

- `build_dataset()` rebalances **after** the eval holdout and the val split are taken and **the train split only**. Eval and val keep the natural distribution — they measure the real task, and a rebalanced exam would flatter the model. A test builds the same corpus with and without rebalancing and requires the eval, val and eval-metadata files to be byte-identical.
- It works only by **dropping whole examples** — never by splitting a cycle or editing a target, which stays the true corrected action set for its prompt. Controls (keyword arguments, surfaced as `build-corpus` flags), applied in this order:
  1. *missed-move cap* (`--missed-move-cap`, **off by default**) — "missed move" labels are ~79% of the directional signal and the noisiest (was a +6% move knowable from that prompt?). When set, examples whose *every* directional label is a missed move are dropped until the share is under the cap; mixed examples are never dropped, so real entries are never lost with the noise. Off for batch 4; the manifest records the share so a later batch can test it.
  2. *direction balance* (`--direction-ratio`, default **1.25**) — if examples containing a BUY and examples containing a SHORT differ by more than the ratio, the majority side's single-direction examples are downsampled toward parity (natural: 3,053 vs 4,001).
  3. *max empty-target fraction* (`--max-empty-fraction`, default **0.10**) — examples whose target is `{"trades": []}` are capped at 10% of the train split (natural: ~21%). Some are kept: "nothing here is worth trading" is a real answer. This step runs **last** because the two before it remove action-bearing examples, which would push the empty share back over the cap had it run first.
  - Sampling is deterministic from `build_dataset()`'s `seed`. `--no-rebalance` leaves the natural mix.
- Among empty-target examples, **those containing at least one losing entry are kept before any made only of flat easy negatives** — they carry the discrimination lesson (§3, fact 1). That needs each label's origin: `label_origin()` derives it from the AI's own signal plus the corrected label (`kept_win` / `lost_entry` / `flat_hold` / `missed_move`), and `build_cycle_example()` carries it in `_meta["origins"]`. `_meta` is stripped on write, so origin never reaches a training file; it does reach the exam's metadata, and the exam reports accuracy per origin.
- The manifest reports, before and after rebalancing (`rebalance.train_before` / `train_after`) and for val and eval: example count, empty-target fraction, mean trades per target, examples with a BUY / with a SHORT, label distribution, label-origin distribution, and the missed-move share. A rebalance that cannot be audited from the manifest is not done.
- There are **no** per-example loss weights: HOLD is an omission, there are no HOLD tokens to weight.

### 4.2 Prompt masking and the learning-rate schedule — `build_train_command()`, `build_train_config()` in `finetune/local_train.py`

*Failures answered:* (a) the answer was 0.4–0.7% of the loss in every batch (§0.1); (b) batch 3's constant 1e-5 destabilized past ~800 steps — validation loss went from 0.82 to 7.3.

- **Prompt masking is always on.** `build_train_command()` always passes `--mask-prompt`: the loss covers the assistant answer only. There is no flag to turn it off. This is the single largest change in batch 4 — it moves the answer from under 1% of the training signal to all of it.
- **Learning rate:** linear warmup over the first 5% of iterations to a peak of **1e-5**, then cosine decay to **1e-7** at the final iteration (`--learning-rate`, `--end-lr`, `--warmup-fraction`). mlx-lm 0.31.3 accepts a schedule only through its config file, so `cmd_train` writes `train_config.yaml` into the adapter directory and passes `--config`; `build_train_command()` and `build_train_config()` stay pure and are pinned by tests. mlx-lm joins the warmup and the decay end to end, so the cosine's step count is the iterations left after the warmup (1,200 iterations → 60 warmup + 1,140 decay).
- The config is written with plain decimals and **read back through a YAML parser before launch**: stock YAML reads `1e-05` as a string, which would reach the optimizer as text. If the file does not read back as exactly the intended numbers, training refuses to start.
- The same config sets checkpoint and validation intervals to every **100** steps (so every saved checkpoint has a validation loss at the same step) and the validation sample to **100** examples (`--val-batches`; mlx-lm's default is 25). With masking on, a validation pass scores only answer tokens — a few dozen per example — and 25 examples is too few to rank checkpoints.
- Defaults now match what every batch actually ran: the 4-bit base `mlx-community/Qwen2.5-7B-Instruct-4bit` and batch size 1. They previously named the unquantized repo and batch size 2, and every real run overrode them by flag — a run without flags would have trained a different model.

### 4.3 Mid-run checkpoints are first-class candidates — `select_checkpoints()`, `cmd_eval()` in `finetune/local_train.py`

*Failure answered:* evaluating only the final adapter threw away the best model (batch 3's final step was the collapsed one).

- `cmd_train` tees the trainer's output to `train.log` in the adapter directory; mlx-lm writes numbered checkpoint files beside `adapters.safetensors`.
- `eval --adapter <run dir> --sweep` examines the **three checkpoints with the lowest validation loss, plus the final one** (`--sweep-top` changes the three; `--steps 400,600` names steps explicitly instead). A checkpoint whose validation loss is NaN or infinite sorts last, never first. The **base answers once** and every checkpoint is scored against those same generations. Each checkpoint is staged as `adapters.safetensors` beside the adapter config in its own temporary directory, because that is what the mlx-lm loader reads. `--sweep` without a training log fails loudly rather than guessing.
- The report keeps every checkpoint's scores and raw generations (doctrine item 2 in doc 27: an unexplained score is forensically worthless), names the winner, and records that the winner was chosen on the exam from N candidates — picking the best of several on the exam flatters it slightly; the lowest-validation-loss step is the choice made without looking at the exam, and is recorded beside it.

### 4.4 Guessing baselines and the promotion bar — `baseline_scores()`, `paired_comparison()`, `promotion_bar()` in `finetune/local_train.py`

*Failure answered:* "beats the base" can hide behind class priors (batch 3's exam: ~33% is what label-frequency guessing scores), and a one-point "win" can be noise (batch 2).

- Every exam reports three baselines computed from the exam's own labels: a **frequency-matched guesser** (each answer drawn from the exam's label frequencies; mean and 5th–95th percentile band over 1,000 seeded draws, with the closed-form expectation Σp² beside it), **always HOLD**, and **always the majority class**.
- Every score reports per-class accuracy, the **answer mix** (how often the model answered bullish / bearish / HOLD / option / unparseable), and accuracy **per label origin**.
- Each checkpoint gets a **paired comparison** against the base on the same decisions: how many only the adapter got right, how many only the base got right, and the exact two-sided sign-test p-value. Because twelve replicate profiles judge the same stock on the same day against the same outcome, those rows are one piece of evidence, not twelve: the comparison is also computed **per distinct stock-and-day** (each votes once, for whichever answerer got more of its decisions right), and that `clustered_p_value` is the one the bar trusts. The report states how many distinct stock-days the exam really contains.
- **The promotion bar** (`passed`): the checkpoint beats the untrained base on overall accuracy, **and** beats the 95th percentile of the frequency-matched guesser, **and** is not worse than the base on *both* directional classes (a model that wins overall by answering HOLD is batch 2 again). `clear_win` additionally requires the clustered paired test to put the win beyond chance (p < 0.05); it is reported beside `passed` so the operator sees how solid a pass is. Every failure lists its reasons in plain sentences.

### 4.5 Over-length examples — `fit_example()`, `fit_split()` in `finetune/dataset_builder.py`

*Failure answered:* 31–40% of training examples were longer than the 8,192-token window; mlx-lm keeps the first 8,192 tokens and drops the rest, and the answer is at the end — so those examples trained on no answer at all (§0.1).

- **Nothing longer than the window is ever written** (`--max-tokens`, default 8,192), in any split. `fit_split()` asserts it after the pass; the manifest records the longest surviving example.
- Lengths are measured with the **base model's own tokenizer** when one is installed (the Mac's training venv): `build-corpus` passes an exact counter that counts the way the trainer does. Where no tokenizer exists (the droplet), the builder falls back to a conservative character estimate — **2.6 characters per token**, below the measured minimum of 2.68 (5th percentile 2.76, median 3.02), so it can only over-count: it may split an example that would have fit, never pass one that does not. The manifest records which method measured the corpus.
- An over-length example is **split along its candidate table**. The splitter parses the table by the prompt builder's own grammar (section headers, numbered candidate blocks with the past-cases and rule-panel blocks they own, section footers, trailing pair blocks), packs candidates in their original order into the fewest parts that fit, and evens the parts out. Every part keeps the **full shared context** — portfolio, market, pair blocks, rules — and its target and metadata are restricted to the candidates it shows. The fixed context is at most ~5,600 tokens and a candidate block ~1,060, so parts typically carry three or more candidates.
- The parser is strict: if the table cannot be located, if a top-level line is one it does not recognise, if an owned block names a different symbol, or if its parse does not reassemble to the original text **byte for byte**, the example is **dropped and counted** — never emitted truncated. So is an example where one candidate alone exceeds the window, or where a labeled symbol has no candidate block. Verified against all 9,450 batch-3 training prompts: every one parses and round-trips exactly.
- A part that shows **no labeled candidate is not emitted**: an empty target there would assert "no trade" about candidates whose right answer is unknown.
- The length pass runs **after** the cycle-level train/val/eval assignment, so a cycle's parts can never straddle splits (they share most of their prompt; a part leaking into val or the exam would contaminate it).
- Manifest (`length`): the measuring method, the window, and per split — examples in, how many fit / were split / were dropped, parts emitted, labeled decisions lost to drops, and the longest example after the pass.

### 4.6 Target hygiene — `_corrected_trade_dict()` in `finetune/dataset_builder.py`

*Failure answered:* 18% of batch-3 target trades carried internal bookkeeping keys (`_ledger_rar`, `_ledger_best_rar`, `_ledger_best_expr`, `_ledger_is_override`) that the opportunity ledger stamps onto the stored response *after* the AI answers. The model was being taught to invent them. Keys with a leading underscore never reach a target.

### 4.7 Refuse-to-train guard — `check_corpus_lengths()`, `cmd_train()` in `finetune/local_train.py`

*Failure answered:* with masking on, an example whose answer lies outside the window has **zero** loss tokens, and mlx-lm's loss divides by that count — a NaN that destroys the adapter at batch size 1. Before launching, `train` tokenizes the train and validation files exactly as the trainer will and **refuses to start** if any example exceeds the window or has no answer token inside it. It names the offending examples and tells the operator to rebuild the corpus for that window.

### 4.8 Snapshot guard — `check_profile_snapshots()` in `finetune/local_train.py`

*Failure answered (before it happened):* the builder only warns on an unreadable journal and moves on, so a torn or half-copied snapshot would silently shrink the corpus by a whole profile. `build-corpus` refuses if any journal copy fails SQLite's integrity check, is unreadable, or holds no resolved predictions; and the manifest reports labeled decisions **per source profile**, so a profile contributing nothing is visible.

### 4.9 Time-ordered split with a purge — `build_dataset()` in `finetune/dataset_builder.py`

*Failure answered (before it could flatter a result):* a label is a **forward return**, known only when the prediction resolves, days after the decision. Twelve replicate profiles see largely the same symbols at the same moments. With the exam being the most recent 200 cycles and everything older available for training, a training example decided the day before the exam window carried a label that was earned *inside* it ("that stock fell that week"). An adapter trained on it could score on the exam from memorised symbol-and-week outcomes the untrained base never saw — a leak that flatters exactly the comparison the promotion bar rests on. The old validation split had the same problem worse: it was a *random* sample of the training pool, so validation loss would reward memorisation, and checkpoints are ranked on it.

- The split is ordered in time: **train │ val │ exam period (newest)**.
- **The exam spans several days** (`--eval-days`, default **3**). Outcomes take 5–8 days to resolve (up to ~15), so "the 200 most recent resolved cycles" turned out to be the decisions of a *single trading day* — one market regime, seen by twelve profiles at once (its labels ran 213 SHORT to 63 BUY because the market fell the following week). The exam is now `--eval-holdout` cycles (default 200) sampled evenly, with the build seed, across the last three decision dates that have resolved outcomes. The rest of that period is simply unused — it cannot be trained on.
- Val is the block immediately before the exam period (`--val-fraction`, default **1%** from the driver, ~160 cycles). Training is purged a full outcome horizon before it, so every validation cycle costs training data; a validation pass scores only 100 examples.
- **Purge:** any *train* example whose last label resolved at or after the first decision of what follows it (val, or the exam period when there is no val) is dropped and counted. Val is **not** purged against the exam — it is never trained on, and with a 5–8 day horizon purging it emptied it on the first real build. Each example's `resolved_at` (the latest among its labels) is carried in metadata for this.
- The manifest's `split` block records the boundaries, the cycles available on each exam-period day, and how many training cycles the purge removed.

### 4.10 Failed AI calls are not decisions — `_is_failed_call()` in `finetune/dataset_builder.py`

*Failure answered:* when the apex call fails (provider 429/5xx) or is cost-capped, `ai_analyst` returns a stand-in (`{"trades": [], "portfolio_reasoning": "AI call failed: …"}`) and the pipeline journals a HOLD prediction for every candidate of that cycle — decisions the model never made. Experiment 2's first four weeks hold **8,187** of them (the Gemini arms lost ~21% of their cycles to quota errors; see OPEN_ITEMS). They had stayed out of the corpus only by accident — such cycles store no prompt, which is also why the corpus is 22,395 Experiment-2 decisions rather than the ~27,000 a label-only count suggests. The quality filter now excludes them on purpose, recognising the stand-in by its shape (no trades, reasoning that *begins* with the failure marker, or the cost-cap flag) so a real answer that merely mentions a failure is not mistaken for one.

### 4.11 Unlabeled candidates are not taught as HOLD — `prune_unlabeled()` in `finetune/dataset_builder.py`

*Failure answered:* the target expresses HOLD by **omission**, so every candidate a prompt shows but the target does not list is taught as "no trade" — including candidates with **no label at all**: moves in the ambiguous 2–5% zone, scratch outcomes, outcomes that never resolved cleanly. The labeling rules exclude those rows precisely so the model is taught *nothing* about them; leaving their blocks in the prompt taught HOLD anyway. About 29% of shown candidates were unlabeled — a systematic push toward the blanket-HOLD behaviour batches 2–3 showed, and one that survives masking (found reading real batch-4 examples before training: a part showing two stocks, one labeled HOLD and one unlabeled, with the target `{"trades": []}`).

- For **train and val**, every candidate block without a label is removed from the prompt (with the past-cases and rule-panel blocks it owns; a section left with no candidates is dropped with its footer), using the same strict parser as the splitter. Every target is then exactly true of what its prompt shows. It runs before the length pass — a pruned prompt is shorter and splits less.
- The **exam keeps whole prompts**: it only ever grades labeled symbols, and it should look like production.
- An example whose table the parser cannot read is left whole and counted. The manifest's `prune` block reports examples pruned, candidates removed and unparseable examples per split. `--keep-unlabeled` turns it off.

## 5. Tests (suite is zero-fail, zero-skip)

`tests/test_finetune_batch4_recipe_2026_09_20.py` pins every item above:

- Label origin: each of the four origins from its (signal, outcome, return) inputs, the no-shorting and option cases; origin present in metadata and absent from every written training file.
- Target hygiene: underscore keys never reach a target; sizing fields survive.
- Over-length: the parser round-trips the production grammar; a short example is untouched; a long one splits into fitting parts that keep the full shared context and whose labels and trades partition the original exactly; a part with no labeled candidate is not emitted; an unrecognised top-level line, an owned block naming another symbol, a prompt with no table, and a single over-window candidate are all refused or dropped and counted; nothing over the limit is written to any split; the character estimate never under-counts the calibration.
- Rebalance: empty-target cap honoured with some empties surviving; losing-entry empties preferred; direction balance; no target altered and no cycle duplicated; deterministic under a seed; missed-move cap off by default and correct when set; train-only, with eval / val / eval-metadata byte-identical with and without rebalancing; manifest carries before and after; the look-ahead guard still refuses a leaking row.
- Training: defaults are what every batch ran; the command always masks the prompt and passes the config; the schedule is warmup plus cosine to the last step; the rendered config reads back as numbers; the length guard flags over-length and answerless examples; `train` refuses a damaged corpus without launching anything.
- Snapshot guard: torn, empty and table-less journal copies are named; the build refuses; per-source counts are in the manifest.
- Time-ordered split: blocks are ordered train, then val, then eval; a training label learned inside a later block is purged and counted; val is not purged against the exam; the exam is sampled evenly across the last decision days, deterministically, and nothing from the exam period is trained or validated on.
- Pruning: unlabeled candidate blocks (and what they own, and emptied sections with their footers) leave the prompt while labeled ones, the shared context, the answer and the metadata stay; fully-labeled and unparseable examples are left alone; train and val are pruned while the exam keeps whole prompts; the switch turns it off.
- Independence: twelve replicate rows of one stock-day count as one piece of evidence — a row-level p-value that looks decisive does not make a clear win.
- Failed calls: the stand-in responses are recognised; a real "pass" and a real answer that mentions a failure are not; a failed cycle never becomes a HOLD lesson even if it stored a prompt.
- Checkpoint sweep: discovery, selection by validation loss, a blown-up checkpoint never ranked first, staging, base answers generated exactly once across a four-checkpoint sweep, and `--sweep` without a log fails loudly.
- Baselines and bar: frequency-matched mean matches the closed form; always-HOLD and majority baselines; determinism; a HOLD-collapsed answer set that wins overall does **not** pass; losing to the base or to guessing fails with reasons; a real discriminating win passes and is clear; a narrow pass is not called a clear win; an empty exam never passes.

`tests/test_learning_own_model_panel_2026_09_20.py` pins the Learning-page panel: the shipped status record is complete, says not in use, matches doc 27's numbers, and is plain English; a missing or malformed record is an error, not an empty panel; the page renders the real status, and renders "unavailable" when the record cannot be read.

## 6. Running batch 4 (operator, on the Mac)

```bash
# 1. Snapshot the corpus from the droplet (read-only against the source)
mkdir -p ~/Quantops-finetune/corpus/profile_dbs
rsync -az root@67.205.155.63:/opt/quantopsai/backups/predictions_archive/ \
      ~/Quantops-finetune/corpus/predictions_archive/
# The journals are live SQLite files the scheduler writes to: a plain file
# copy can catch one mid-write. Take a consistent copy of each with SQLite's
# online backup into the droplet's RAM disk, pull it, remove it - one at a
# time (~100MB each; the droplet's disk is nearly full and its RAM is small).
for pid in 229 230 231 232 233 234 235 236 237 238 239 240; do
  ssh root@67.205.155.63 "mkdir -p /dev/shm/qo-ft && /opt/quantopsai/venv/bin/python3 -c \"
import sqlite3
src = sqlite3.connect('file:/opt/quantopsai/quantopsai_profile_${pid}.db?mode=ro', uri=True)
dst = sqlite3.connect('/dev/shm/qo-ft/quantopsai_profile_${pid}.db')
src.backup(dst); dst.close(); src.close()\""
  rsync -az root@67.205.155.63:/dev/shm/qo-ft/quantopsai_profile_${pid}.db \
        ~/Quantops-finetune/corpus/profile_dbs/
  ssh root@67.205.155.63 "rm -f /dev/shm/qo-ft/quantopsai_profile_${pid}.db"
done

# 2. From an up-to-date checkout of the repo on the Mac, with the training venv:
PY=~/Quantops-finetune/venv/bin/python
$PY -m finetune.local_train build-corpus                 # prints the manifest; read it
$PY -m finetune.local_train train --data <corpus dir>    # 1,200 steps, masking on, cosine schedule
$PY -m finetune.local_train eval  --data <corpus dir> --adapter <adapter dir> --sweep
```

Use the training venv's Python (it has mlx-lm and the tokenizer); the repo's own venv has neither. Keep the journal file names intact — the builder derives each profile's dedup namespace from `quantopsai_profile_<id>.db`, and a renamed file would let one profile's rows swallow another's (the batch-3 dedup bug). Copy only profiles 229–240; older profile DBs on the droplet are stale. `build-corpus` refuses if any journal copy fails its integrity check, and `train` refuses if any example would lose its answer to the window.

**What a run can see, and how long it takes (measured on batch 4).** A training step takes about 26 seconds at batch size 1 (the forward and backward pass still cover the whole prompt; only the loss is restricted to the answer), and each 100-example validation pass about 18 minutes. A 1,200-step run is therefore ~8.5 hours of training plus 13 validation passes (~4 hours): **12–13 hours**, and it sees about 1,200 of the ~16,000 training examples — a fraction of the corpus, not a pass over it. Peak memory ~22.6GB of 64GB; gradient checkpointing stays on. The exam generates ~40 seconds per prompt for the untrained base (it writes long answers) and less for an adapter, so a four-checkpoint sweep over ~430 exam prompts is roughly half a day more. The checkpoint sweep, not the step count, picks the model.

**Before trusting any number:** open the manifest and confirm the empty-target fraction and label-origin mix are what §4.1 says they should be; open a handful of training examples and read them; after eval, read ten raw generations from the winning checkpoint. Batches 1–3 each had a defect that only reading the artifacts revealed.

## 7. Definition of done

1. The five changes are merged with their tests; full suite green; CHANGELOG entry.
2. Batch 4 has been trained and examined on the Mac, and `docs/27_FINETUNE_TRAINING_LOG.md` has its entry — corpus and rebalance numbers, loss curve, the checkpoint sweep table against base / random band / always-HOLD, the answer mix, and a verdict.
3. If the bar in §4.4 is cleared: stop and bring the result to the operator — hosting, the shadow seat and any spend are their decision (`docs/25_MODEL_SELECTION_AND_LEARNING_PLAN.md` step 4.5). If it is not cleared: the log entry names the specific next change (the missed-move cap from §4.1 is the first candidate), and that change is built before batch 5 — never retrain on the same recipe and near-identical data.
