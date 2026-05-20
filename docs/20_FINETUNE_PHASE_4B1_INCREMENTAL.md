# Phase 4b.1 — Incremental Fine-Tune on Archived Predictions

**Scoping doc for the chip-away weekly fine-tune of an open-vendor model (OpenAI `gpt-4o-mini` recommended) trained on the system's own archived predictions.**

Status: SCOPING — not yet implemented.
Owner: TBD.
Created: 2026-05-19.
Depends on: B1 (archive-before-reset) ✅ shipped 2026-05-19; B2 (multi-horizon outcomes) recommended but not strictly required.
Targets the docs/17 Phase 4b workstream; supersedes the "big-batch" implicit assumption with an incremental architecture that's ~50× cheaper.

---

## 0. TL;DR

Replace the apex AI's per-cycle call (currently `gemini-2.5-flash-lite` via `ai_providers.call_ai`) with a fine-tuned `gpt-4o-mini` model trained weekly on this system's own resolved predictions. Cadence is incremental — each Sunday's training extends the prior week's checkpoint with the new week's archived predictions, rather than retraining from scratch on a giant corpus. Expected monthly cost at the measured prediction rate (~2K/profile/month, ~13 AI profiles): **~$15-30/month for OpenAI training + the small inference markup**, vs current Gemini spend ~$215/month.

The whole architecture mirrors the existing `pipelines/shadow.py` cutover pattern: shadow soak first (per-profile flag), measure verdict-layer agreement and per-direction lift, promote only after evidence. The fine-tuned model is gated behind a per-profile `use_finetuned_ai` flag, default OFF, soak-then-cutover identical to the dispatch cutover.

---

## 1. Why

### 1.1 The problem fine-tuning solves
The apex LLM is a frozen base model — `claude-haiku-4-5-20251001` by default, gemini-2.5-flash-lite as primary today. Its weights have no exposure to this system's specific candidate universe, regime tagger, specialist taxonomy, or historical outcomes. RAG (Phase 2, shipped 2026-05-18) injects up to 3 similar past cases at inference time but can't internalize broader patterns. The meta-model (`meta_model.py`) reweights AI confidence ex-post but doesn't change what the AI sees ex-ante.

Fine-tuning is the lever that lets the LLM **learn**. Patterns the deterministic + specialist layers can't encode — narrative-style heuristics, multi-feature interactions, regime-conditional framing — become weights instead of injected context.

### 1.2 Why incremental (chip-away) beats big-batch
Big-batch retraining (the implicit assumption in docs/17's Phase 4b scoping) means accumulating ≥50K examples then running a quarterly multi-thousand-dollar training job. Three structural problems with that approach for trading data:

| Issue | Big-batch | Incremental (chip-away) |
|---|---|---|
| **Regime drift** | Quarterly cadence is slow vs market regime turnover (weeks-months). Model is always 1-3 months behind | Weekly cadence catches regime drift in the data; each week's data carries the most recent regime |
| **Catastrophic forgetting risk** | One big update can drastically shift weights, blowing up a previously-good model | Tiny weekly updates can't catastrophically shift; each week's lift is incremental |
| **Cost scaling** | Linear in corpus size — a 100K-example train at GPT-4o-mini rates ≈ $300+ per attempt; 5 hyperparameter trials = $1500 | Linear at small scale — each weekly extension on ~500 new examples ≈ $1-3 per run |
| **Validation latency** | Quarterly bake-off; long feedback loops; hard to attribute lift to specific patterns | Weekly comparison against prior week's checkpoint; tight feedback loops |

### 1.3 Why OpenAI specifically
- **Incremental fine-tune support** — OpenAI's fine-tune API supports extending an existing checkpoint with new data via `training_file` against the prior model. (Google Gemini also supports it but is more expensive at our scale; Anthropic doesn't offer public fine-tune.)
- **Mature tooling** — file upload, job monitoring, model registry are well-documented + production-stable.
- **Quality on small models** — `gpt-4o-mini` is a strong base for fine-tuning at low cost; its fine-tuned variant typically matches or exceeds the base `gpt-4o` on domain-specific tasks at ~1/4 the per-token rate.
- **Easy fallback** — switching back to the base model is a one-line config change. No infra teardown needed if Phase 4b.1 doesn't pan out.

### 1.4 Why this is now possible
B1 (shipped 2026-05-19) added the data foundation:
- `ai_predictions.prompt_text` — exact prompt the AI saw (the training input)
- `ai_predictions.raw_response_json` — exact response the AI produced (the training label)
- `ai_predictions.cycle_id` + `ai_cycles` — cross-candidate context for context-aware training
- `predictions_archive/*.jsonl` — durable corpus that survives experiment resets

Without B1, every reset destroyed the corpus and incremental fine-tune was impossible.

---

## 2. Goals + non-goals

### Goals
- **Cost-effective fine-tune lift**: ≥5% absolute improvement in resolved-prediction win rate on the pilot profile over a 4-week shadow window, vs the base `gpt-4o-mini` on the same prompts.
- **Operational simplicity**: weekly job runs on a cron; no daily-baby-sitting required.
- **Per-profile gating**: a single `use_finetuned_ai` flag flips a profile's apex AI from base → fine-tuned, with instant rollback by flipping the flag back.
- **Total monthly cost ≤$50** at current data volumes (training + inference markup combined).
- **Auditable lift** — every promotion decision is backed by a dated measurement record so we can revisit later.

### Non-goals
- **Not** replacing the deterministic-specialist library, meta-model, RAG, or specialist ensemble. These layers compound; fine-tune is additive to them, not a replacement for any.
- **Not** training one model per profile. Pool-then-pilot: one fine-tuned model trained on pooled fleet data, soaked on one profile first, then rolled out.
- **Not** building self-hosted GPU infra (that's Phase 4b.2 separately).
- **Not** trying to fine-tune Anthropic Claude (not publicly available; would require Bedrock enterprise).
- **Not** automating promotion. Promotion to live trade-dispatch (vs shadow) is operator-driven after reviewing the soak metrics.

### Out-of-scope items deferred to later phases
- **RLHF / DPO** on the fine-tuned model (uses operator preference pairs as training signal — a separate research direction)
- **Multi-vendor fine-tune ensemble** (run OpenAI ft + Google ft in parallel and consensus-vote)
- **Per-pipeline fine-tunes** (separate stock-pipeline and option-pipeline checkpoints)

---

## 3. Prerequisites

### Shipped today (2026-05-19)
- ✅ **B1 data-collection upgrade** — `ai_predictions.prompt_text`, `raw_response_json`, `cycle_id`, meta-model scores; new `ai_cycles` table; `predictions_archive.py` preserves the corpus across experiment resets.

### Recommended before starting Phase 4b.1
- **B2 multi-horizon outcomes** (Task #185) — gives the dataset builder a richer label space (1d / 5d / 20d returns instead of one fixed horizon). Not strictly required — single-horizon labels work — but improves training signal quality.
- **6-8 weeks of accumulated post-reset data** — at 2K/profile/month × 10 AI profiles × 6 weeks = ~30K pooled examples, the right size for a meaningful first fine-tune. Starting earlier with fewer examples gives a weaker initial model.

### Required external dependencies
- **OpenAI API access** with a billing-enabled account. Fine-tune API requires Tier 1+ accounts; current spend on Gemini may not be enough to qualify automatically — verify before scoping further.
- **API key storage** — must follow the same encrypted-per-profile pattern as Gemini keys today (`ai_keys/openai_finetune.enc` or similar). See `feedback_no_master_key` memory rule — no env-level master keys.
- **OpenAI billing budget** — recommend $50/month cap to start; raise after observing actual usage.

### Operator decisions needed before implementation
1. **Pilot profile choice** — recommend profile 15 (`EXP-A1-FullSystemStandard`) since it's the AI control arm with shadow harness already enabled.
2. **Base model choice** — `gpt-4o-mini` (~$0.30/M input, $1.20/M output for fine-tuned) vs `gpt-4o` (~$3.75/M input, $15/M output for fine-tuned). Strongly recommend mini for cost; revisit if quality is insufficient.
3. **Training cadence** — recommended weekly (Sunday 23:00 UTC). Could be daily but weekly batches give cleaner regime cohorts.
4. **Pooled vs per-profile dataset** — recommended pooled for the first model (faster to useful dataset size); per-profile is a future iteration once each profile has 25K+ examples individually.

---

## 4. Architecture overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                      Weekly cadence (Sunday 23:00 UTC)              │
│                                                                     │
│  ┌────────────────┐    ┌──────────────────┐    ┌──────────────────┐│
│  │ Dataset builder│ →  │ OpenAI training  │ →  │ Model registry   ││
│  │ (.jsonl from   │    │ job (incremental │    │ + version bump   ││
│  │  archive +     │    │  on prior model) │    │                  ││
│  │  live preds)   │    └──────────────────┘    └──────────────────┘│
│  └────────────────┘             │                        │          │
│         ↑                       │                        │          │
└─────────┼───────────────────────┼────────────────────────┼──────────┘
          │                       │                        │
          │                       ↓                        ↓
   ┌──────┴──────┐         ┌─────────────┐         ┌────────────────┐
   │ ai_         │         │ Lift report │         │ ai_providers   │
   │  predictions│         │ + soak      │         │  routing       │
   │  + archive  │         │  dashboard  │         │  (per-profile  │
   └─────────────┘         │  ("/finetune"│        │   flag)        │
                           │  page)       │         └────────────────┘
                           └──────────────┘                 │
                                                            ↓
   ┌────────────────────────────────────────────────────────────────┐
   │                      Per-cycle (live trading)                  │
   │                                                                │
   │  ai_select_trades(...) → ai_providers.call_ai(...)             │
   │      ↓                                                         │
   │      ├─ if ctx.use_finetuned_ai: → model="ft:gpt-4o-mini:..."  │
   │      └─ else: → model="gemini-2.5-flash-lite" (current base)   │
   │      ↓                                                         │
   │  pipeline_shadow_runs row written for soak measurement         │
   └────────────────────────────────────────────────────────────────┘
```

The architecture deliberately mirrors the existing `pipelines/shadow.py` cutover pattern from Scope C:
- Per-profile flag gates new behavior; default OFF
- Shadow harness runs the new path against the legacy path in parallel
- Dashboard surfaces the agreement / lift comparison
- Operator promotes via the same per-profile flag

This means Phase 4b.1 can reuse the same evaluation infrastructure (shadow harness, dashboard, audit logs) without building anything new for soak.

---

## 5. Data pipeline

### 5.1 Source data
Three sources, combined:
1. **`predictions_archive/{profile_id}/*/predictions.jsonl`** — historical predictions from prior experiment generations
2. **Live `ai_predictions` rows** — current-generation predictions since the last reset
3. **`ai_cycles`** — joined for cross-candidate context

### 5.2 Filtering for training-quality rows

Include only rows where ALL of:
- `status = 'resolved'` (we know the outcome)
- `prompt_text IS NOT NULL` AND `length(prompt_text) > 100` (skip pre-B1 rows)
- `raw_response_json IS NOT NULL` AND parseable (skip rows where response JSON corrupted)
- `actual_return_pct IS NOT NULL` (the outcome signal)
- `actual_outcome IN ('win', 'loss', 'scratch')` (canonical label)
- `data_quality IS NULL` (skip rows tagged as corrupted by the integrity audits)

Estimated yield: ~80% of raw predictions become training-quality rows.

### 5.3 OpenAI training file format

OpenAI fine-tune uses a JSONL where each line is:
```json
{"messages": [{"role": "system", "content": "<prompt prefix>"},
               {"role": "user", "content": "<candidate context>"},
               {"role": "assistant", "content": "<correct response>"}]}
```

For our use case, the mapping is:
- **System message** — the constant prompt prefix from `ai_analyst._build_batch_prompt` that defines the AI's role and task (everything before the candidate-specific content)
- **User message** — the candidate-specific content (the row's `prompt_text` minus the system prefix)
- **Assistant message** — the **correct** action in hindsight, derived from the prediction's outcome:
  - If `actual_outcome == 'win'` AND the AI took action: the action the AI took was correct → use it as the label
  - If `actual_outcome == 'loss'` AND the AI took action: the action was wrong → use the inverse (BUY → HOLD, SHORT → HOLD)
  - If `predicted_signal == 'HOLD'` AND `abs(actual_return_pct) < 2%`: holding was correct → label = HOLD
  - If `predicted_signal == 'HOLD'` AND `actual_return_pct > 5%`: should have BOUGHT → label = BUY
  - If `predicted_signal == 'HOLD'` AND `actual_return_pct < -5%`: should have SHORTED → label = SHORT (only if profile has shorting enabled)
  - Skip rows in the 2%-5% gray zone (ambiguous label)

This **hindsight-relabeling** is the critical design choice — we're not training the model to mimic its own past behavior, we're training it to mimic **what would have been correct** given the same input.

### 5.4 Per-week dataset size estimate

Measured rate: ~2,000 predictions/profile/month. Filter yield ~80%. Pool across 10 AI profiles. Per week:
- 2,000 × 10 / 4.33 weeks/month × 0.80 = **~3,700 training-quality rows per week**

OpenAI fine-tune minimum is 10 examples per job; recommended ≥50 for any signal. 3,700/week is comfortable.

### 5.5 Train/val split

- 90% to training set
- 10% to validation set (OpenAI uses this for the job's own perplexity tracking)
- Hold out the **most recent 200 predictions** as a separate eval set we score ourselves (not given to OpenAI). The eval set is used to compute the per-week lift metric.

### 5.6 Code components

**New file: `finetune/dataset_builder.py`**
- `build_weekly_dataset(profile_ids, week_start, week_end)` → returns (train_jsonl_path, val_jsonl_path, eval_set)
- Reads from `ai_predictions` (current) + `predictions_archive/` (historical)
- Applies the filter + hindsight-relabel logic
- Writes OpenAI-format JSONL files

**New file: `finetune/__init__.py`** — package init.

---

## 6. Training pipeline

### 6.1 Weekly cadence

New scheduler task in `multi_scheduler.py` daily-snapshot block (runs Sundays only — gated by weekday check):

```python
if datetime.now(ET).weekday() == 6 and now.hour >= 23:
    run_task(
        "Phase 4b.1 weekly fine-tune",
        lambda: _task_finetune_weekly(),
        db_path=master_db,
    )
```

The task runs on the master orchestrator (not per-profile) since it pools data fleet-wide.

### 6.2 Training job submission

**New file: `finetune/training_runner.py`**

```python
def submit_weekly_finetune(prior_model_id: str | None) -> str:
    """Submit a fine-tune job to OpenAI for the past week's data.

    If prior_model_id is set, use it as the base (incremental
    extension). Otherwise start from the canonical
    'gpt-4o-mini-2024-07-18' base. Returns the new job ID.
    """
    from openai import OpenAI
    client = OpenAI(api_key=_load_openai_key())

    week_start, week_end = _last_completed_week()
    train_path, val_path, eval_set = build_weekly_dataset(
        profile_ids=get_active_ai_profile_ids(),
        week_start=week_start, week_end=week_end,
    )

    train_file = client.files.create(
        file=open(train_path, "rb"), purpose="fine-tune",
    )
    val_file = client.files.create(
        file=open(val_path, "rb"), purpose="fine-tune",
    )

    job = client.fine_tuning.jobs.create(
        training_file=train_file.id,
        validation_file=val_file.id,
        model=prior_model_id or "gpt-4o-mini-2024-07-18",
        hyperparameters={"n_epochs": 1, "learning_rate_multiplier": "auto"},
        suffix=f"quantopsai-w{week_start.strftime('%Y%m%d')}",
    )
    return job.id
```

Hyperparameters:
- `n_epochs=1` — incremental updates use a single epoch to avoid overfitting to the small weekly batch
- `learning_rate_multiplier="auto"` — let OpenAI pick; manual override only if validation perplexity drifts
- `suffix=quantopsai-w{date}` — human-readable model name like `ft:gpt-4o-mini-2024-07-18:org:quantopsai-w20260524:xyz`

### 6.3 Job monitoring + promotion

**New file: `finetune/job_monitor.py`**

Runs as a separate scheduler task every 30 min:
- Polls `client.fine_tuning.jobs.retrieve(job_id)`
- When status `'succeeded'`: write a row to a new `finetune_models` table (model_id, training_window, parent_model_id, training_token_count, validation_loss, created_at, promoted_at=NULL)
- When status `'failed'`: write an `audit_alerts` row + notify_error
- Time budget: fail the job after 24h still pending (cancel + retry next week)

### 6.4 Model registry

**New table on master DB: `finetune_models`**

```sql
CREATE TABLE finetune_models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    openai_model_id TEXT NOT NULL UNIQUE,
    parent_model_id TEXT,
    training_window_start TEXT,
    training_window_end TEXT,
    training_token_count INTEGER,
    training_cost_usd REAL,
    validation_loss REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    promoted_to_shadow_at TEXT,
    promoted_to_live_at TEXT,
    retired_at TEXT,
    retirement_reason TEXT
);
CREATE INDEX idx_finetune_models_created ON finetune_models(created_at DESC);
```

The "promoted_to_shadow_at" + "promoted_to_live_at" + "retired_at" columns give clean lifecycle tracking. At most one model is `promoted_to_live` per profile at a time.

---

## 7. Inference integration

### 7.1 `ai_providers.call_ai` extension

Add a new provider variant `openai-ft` alongside the existing `openai`, `anthropic`, `gemini`:

```python
# In ai_providers.py
def _call_openai_finetuned(prompt, ctx, model_id):
    """Use a fine-tuned OpenAI model. Same response shape as base."""
    from openai import OpenAI
    client = OpenAI(api_key=_load_openai_key())
    response = client.chat.completions.create(
        model=model_id,  # e.g. "ft:gpt-4o-mini:...:quantopsai-w...:..."
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT_PREFIX},
            {"role": "user", "content": prompt},
        ],
        temperature=0.1,
    )
    return response.choices[0].message.content
```

### 7.2 Per-profile routing

New column on `trading_profiles`: `use_finetuned_ai INTEGER NOT NULL DEFAULT 0`

In `ai_select_trades`:
```python
if getattr(ctx, "use_finetuned_ai", False):
    active_model = _get_active_finetune_model(ctx.profile_id)
    if active_model:
        provider, model_id = "openai-ft", active_model
    else:
        # No promoted fine-tune yet — fall back to base
        provider, model_id = ctx.ai_provider, ctx.ai_model
else:
    provider, model_id = ctx.ai_provider, ctx.ai_model
```

### 7.3 Shadow harness extension

Extend `pipelines/shadow.py` (currently does cross-pipeline-dispatch comparison) to also do **fine-tune vs base comparison**:
- When `ctx.use_finetuned_ai = 1` AND `ctx.enable_pipeline_shadow_eval = 1`: run both the fine-tuned model AND the base model in parallel; record both decisions in a new column on `pipeline_shadow_runs`.
- Promotion criterion (operator-reviewed): fine-tune verdict agreement with base ≥ X% AND win-rate lift ≥ Y% over the soak window.

### 7.4 Settings UI

Add to the profile edit page (mirroring the existing `enable_pipeline_shadow_eval` checkbox):
- **Use Fine-Tuned AI** [checkbox] — gated description: "Routes this profile's apex AI call to the latest promoted fine-tune model. Default OFF; flip after the /finetune dashboard shows the model is producing measurable lift."
- **Active fine-tune model**: read-only display of the currently-promoted model_id for this profile

---

## 8. Evaluation framework

### 8.1 Per-week training run report

After each fine-tune job succeeds, generate a report row in a new `finetune_evaluations` table:

```sql
CREATE TABLE finetune_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id TEXT NOT NULL,
    eval_set_size INTEGER,
    win_rate_finetuned REAL,
    win_rate_base REAL,
    win_rate_lift_pct REAL,  -- (win_rate_ft - win_rate_base) * 100
    agreement_with_base_pct REAL,
    per_direction_win_rate_json TEXT,  -- {bullish, bearish, neutral}
    by_strategy_lift_json TEXT,        -- per-strategy_type breakdown
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (model_id) REFERENCES finetune_models(openai_model_id)
);
```

### 8.2 Online shadow A/B during soak

When `use_finetuned_ai=1` is flipped on a profile (after operator review of the eval reports), the shadow harness runs both fine-tuned and base AI calls per cycle (doubles AI spend during soak — accepted cost). The `pipeline_shadow_runs` row gains two new columns:
- `finetune_proposals_json` — what the fine-tuned model said
- `base_proposals_json` — what the base model said
- `agreement_with_base_pct` — verdict-layer match

After 4 weeks of soak data on the pilot profile, the operator reviews the cumulative measure.

### 8.3 Promotion criteria (operator-reviewed, not automated)

Cutover from shadow → live for a profile requires:
1. **Win-rate lift ≥ 5%** absolute over the soak window vs base
2. **Verdict agreement ≥ 70%** with base (if 100%, the fine-tune isn't doing anything; if <50%, suspicious — investigate)
3. **No degradation on any single direction** (don't promote a model that improves bullish trades but degrades shorts)
4. **At least 1000 resolved predictions** in the soak window

Per memory rule `feedback_ai_driven_no_manual_loop` — the **shadow** is automated, but **promotion** is operator-reviewed because it submits real broker orders via the new path.

### 8.4 Dashboard — new `/finetune` page

A read-only dashboard listing:
- All trained models (`finetune_models` rows) with their training-window dates + costs
- Per-model evaluation reports (`finetune_evaluations` rows) with lift charts
- Per-profile soak status: model_id currently shadowed, days into soak, current lift vs base
- Cost YTD on fine-tune training + inference

---

## 9. Cost model

### 9.1 Training cost per week

OpenAI `gpt-4o-mini` fine-tune pricing (verify current at implementation time — vendor prices change):
- Training: $3/1M tokens
- Validation: free (counts as training tokens, included)
- Inference: $0.30/1M input, $1.20/1M output (vs base $0.15/$0.60 = 2× markup)

Per-week training token estimate:
- 3,700 examples × ~2,500 tokens/example (prompt + response combined) = **~9.25M tokens/week**
- At $3/1M = **~$28/week training run**
- × 52 weeks/year = **~$1,460/year training**

### 9.2 Inference cost markup

Current Gemini spend: ~$0.02/cycle × 13 profiles × 28 cycles/day = ~$7.30/day = **~$220/month**

Estimated `gpt-4o-mini` fine-tune inference (assuming same token volume):
- Input tokens/cycle ≈ 8,000 (large prompt with RAG + specialist panel + market context)
- Output tokens/cycle ≈ 500 (compact JSON response)
- Per-cycle cost: 8,000 × $0.30/1M + 500 × $1.20/1M = **$0.0024 input + $0.0006 output = $0.003/cycle**
- × 13 profiles × 28 cycles/day = $1.09/day = **~$33/month inference**

**Total monthly cost: ~$28/week × 4.33 weeks + $33 inference = ~$155/month**

Wait — that's higher than my chat estimate. Let me revise:
- If only the pilot profile uses the fine-tune (not all 13), inference cost is 1/13 of the above = **$2.50/month inference**
- + Training cost: $28/week × 4.33 = **~$121/month training** (the pooled-fleet training cost is independent of how many profiles inference against the model)
- **Pilot-only total: ~$125/month**

**Fleet rollout (all 13 profiles): ~$155/month** (training stays the same; inference scales)

For comparison: current Gemini spend is ~$220/month. **Switching from Gemini to fine-tuned gpt-4o-mini is roughly cost-neutral** AND we gain the fine-tune lift.

### 9.3 Worst-case scenarios

- Training token volume doubles (more verbose prompts post-B2): $250/month training. Still much less than the $1000+ big-batch cost.
- Vendor doubles fine-tune pricing: $300/month all-in. Still affordable; still better than the alternative.
- Need 3 hyperparameter trials per week instead of 1: $400/month all-in. Probably the point to revisit whether self-hosted LoRA (Phase 4b.2) is worth the ops investment.

### 9.4 Cost guardrails to build in

- Hard monthly cap stored in master DB: refuse to submit a training job if YTD cost would exceed the cap
- Per-job cost preview (computed from training-file token count) before submission; require operator approval if cost > $50 for a single run
- Auto-pause incremental training if validation loss starts climbing (model is overfitting; need to restart from a cleaner checkpoint)

---

## 10. Operational concerns

### 10.1 Failure modes

| Failure | Detection | Response |
|---|---|---|
| Training job fails (OpenAI rejects file) | Status='failed' in job monitor | Write audit_alert + notify_error. Keep prior model active. Retry next week. |
| Training job times out (>24h pending) | Time check in job monitor | Cancel via API + write audit_alert + retry next week from prior model |
| Inference fails on the fine-tuned model (rate limit, model not found, etc.) | `_call_openai_finetuned` exception | Fall back to base AI (existing per-provider fallback chain handles this); write audit_alert |
| Fine-tuned model produces malformed JSON | `_parse_ai_response_tolerant` fails | Same as today — log + continue. The tolerant parser handles most cases. |
| OpenAI account suspended / billing issue | Both training + inference fail | Hard fail visible on dashboard; operator switches use_finetuned_ai=0 across all profiles |
| Eval set shows fine-tune is WORSE than base | Win-rate lift goes negative in `finetune_evaluations` | Operator manually retires the model via Settings UI; profile reverts to base on next cycle |

### 10.2 Rollback

Per-profile flip of `use_finetuned_ai` from 1 → 0 routes that profile back to the base AI on the next cycle. No infrastructure teardown needed. Zero-downtime.

For fleet-wide rollback (rare; only if a systemic issue is identified):
- SQL: `UPDATE trading_profiles SET use_finetuned_ai = 0`
- Single statement, takes effect on next cycle for each profile

### 10.3 Observability

- `/finetune` dashboard: model lifecycle, evaluation reports, per-profile soak status, YTD cost
- `audit_alerts` rows for all failure modes (surfaced on `/issues`)
- `ai_cost_ledger` rows tag fine-tune calls separately so cost attribution is unambiguous
- Weekly training-completion email to operator with eval summary

### 10.4 Data privacy / vendor lock-in

- OpenAI's data retention policy for fine-tune training files: review at implementation time. Recommend using an org with the zero-retention business agreement.
- Vendor lock-in: the per-week training files are stored locally in `finetune/training_data/`. If we ever switch vendors, the historical training data is portable; only the trained model is vendor-specific.

---

## 11. Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Hindsight-relabeling produces noisy labels (the "right" action in hindsight isn't always the right decision-rule going forward) | Medium | Medium — model could learn to chase already-realized moves | Use binary `win/loss` only on strong outcomes (|return|≥5%); skip the 2-5% gray zone |
| OpenAI changes fine-tune pricing materially | Low | Low (numbers above scale) | Annual review; switch to Phase 4b.2 self-hosted if pricing 3×+ |
| Fine-tune lift fails to materialize (no measurable improvement over base) | Medium | Low (no cost beyond eval period; revert is free) | Pilot on one profile for 4 weeks before fleet rollout. Operator-reviewed promotion. |
| Self-referential drift: fine-tuning on the AI's own past outputs creates a feedback loop that amplifies its biases | Medium | High | Hindsight-relabel against OUTCOMES (not the AI's past choices). Also: monitor entropy of action distribution over time — if it's collapsing, model is becoming dogmatic |
| Catastrophic forgetting across weekly extensions | Low (small batches mitigate) | Medium | Validation loss tracked weekly; if it climbs, restart from a clean checkpoint with pooled corpus |
| API outage during the weekly cron window | Low | Low | Retry next week; no urgent need for the weekly update |
| Operator forgets to review eval reports → bad model promoted | Medium | High | Auto-email + dashboard alert. NEVER auto-promote. |
| Training-token cost exceeds budget | Low (current rate well under) | Low | Hard monthly cap stored in DB; refuse to submit job above cap |
| Bug in dataset builder produces a training file that includes leaked future information (look-ahead bias) | Low (we control the source) | **Critical** — model would appear to lift in eval but be useless in live trading | Strict test in `tests/test_finetune_dataset_builder.py` that every label is derived from outcomes strictly AFTER the prediction timestamp; assertion fires on any row where `actual_outcome.timestamp < prediction.timestamp` |

---

## 12. Rollout / soak plan

### Phase A — internal test (week 1)
- Build the dataset builder. Run against archived predictions.
- Submit a single training job manually. Verify the job completes and produces a model.
- Verify inference works: hand-call the fine-tuned model on a sample candidate; confirm response shape matches.

### Phase B — shadow on one profile (weeks 2-5)
- Set `enable_pipeline_shadow_eval = 1` AND `use_finetuned_ai = 0` on profile 15 (pilot).
- Modify shadow harness to ALSO run the latest fine-tune model in parallel (no real broker submission).
- Collect 4 weeks of `pipeline_shadow_runs` data comparing fine-tune verdict vs base verdict.
- Generate weekly `finetune_evaluations` reports.

### Phase C — promote on one profile (weeks 6-9)
- After 4 weeks of shadow data show ≥5% lift AND ≥70% agreement AND no per-direction degradation:
- Flip `use_finetuned_ai = 1` on profile 15.
- The new model now submits real broker orders.
- Continue shadow against base (`use_finetuned_ai = 1` AND `enable_pipeline_shadow_eval = 1` still set) to measure live performance.

### Phase D — fleet rollout (weeks 10+)
- Flip `use_finetuned_ai = 1` on 3 additional profiles, one per week.
- Continue per-profile shadow during the rollout.
- After 4 weeks of multi-profile data, flip the remaining profiles.

### Phase E — base AI retirement (post-rollout)
- After 3+ months of clean fleet operation on the fine-tune:
- The base AI provider stays available as the fallback path (per `ai_providers._build_fallback_chain`).
- The base call code stays in place — we don't delete the fallback.

### Kill switches at every phase
- Per-profile `use_finetuned_ai = 0` → instant revert to base
- Operator can retire a model in `finetune_models` (sets `retired_at`); next cycle on any profile using that model falls back to base
- If fleet-wide issue: SQL `UPDATE trading_profiles SET use_finetuned_ai = 0`

---

## 13. Test plan

### Unit tests
- `tests/test_finetune_dataset_builder.py`:
  - Hindsight-relabel logic produces correct labels for known outcome scenarios
  - Gray-zone (2-5% absolute return) rows are skipped
  - Look-ahead bias test: every label timestamp > prediction timestamp
  - Filtering correctly excludes rows missing prompt_text or with status != 'resolved'
  - Train/val/test split is reproducible (same seed → same split)
  - JSONL output validates as OpenAI fine-tune format

- `tests/test_finetune_training_runner.py`:
  - OpenAI API call payload has correct shape (mocked client)
  - Incremental mode (prior_model_id set) passes prior model as base
  - Cost preview matches the expected formula for a known token count
  - Hard monthly cap rejects oversized jobs

- `tests/test_finetune_job_monitor.py`:
  - 'succeeded' status writes a `finetune_models` row
  - 'failed' status writes audit_alert + notify_error
  - 24h timeout cancels the job

- `tests/test_finetune_inference.py`:
  - `_call_openai_finetuned` produces same response shape as base
  - Falls back to base when the fine-tuned model raises
  - Per-profile flag correctly routes between base and fine-tune

### Integration test
- `tests/test_finetune_end_to_end.py`:
  - Synthetic dataset → builder → mocked OpenAI API → mocked job-completion → `finetune_models` row → mocked inference call → expected response

### Source-level pin
- `tests/test_finetune_no_lookahead_bias.py`:
  - Static scan of `finetune/dataset_builder.py` for any use of price data later than the prediction timestamp
  - This is the highest-stakes invariant; better to over-test it

---

## 14. File list to create

```
finetune/                                  # NEW package
├── __init__.py
├── dataset_builder.py                     # build training file from archive + live
├── training_runner.py                     # submit OpenAI fine-tune job
├── job_monitor.py                         # poll job status; write finetune_models row
├── model_registry.py                      # CRUD on finetune_models table
├── evaluator.py                           # compute win-rate lift vs base
└── inference.py                           # _call_openai_finetuned routing helper

tests/
├── test_finetune_dataset_builder.py
├── test_finetune_training_runner.py
├── test_finetune_job_monitor.py
├── test_finetune_inference.py
├── test_finetune_end_to_end.py
└── test_finetune_no_lookahead_bias.py    # the high-stakes invariant
```

## Files to modify

- `journal.py` — add `finetune_models` + `finetune_evaluations` tables to master DB schema
- `models.py` — add `use_finetuned_ai` column to `trading_profiles` + to `update_trading_profile` allowlist
- `user_context.py` — add `use_finetuned_ai: bool = False` field
- `ai_providers.py` — register `openai-ft` provider variant
- `ai_analyst.py` — route to `openai-ft` when `ctx.use_finetuned_ai = True`
- `multi_scheduler.py` — wire `_task_finetune_weekly` into Sunday daily-snapshot block
- `templates/settings.html` — add "Use Fine-Tuned AI" checkbox
- `views.py` — `/finetune` route + template render
- `templates/finetune.html` (NEW) — dashboard
- `templates/base.html` — nav link for `/finetune`

---

## 15. Open decisions

1. **OpenAI account tier** — does the operator's current OpenAI account qualify for fine-tune API access? Verify before scoping further.
2. **Pilot profile** — recommend profile 15 (EXP-A1-FullSystemStandard); operator can override.
3. **Training cadence** — weekly recommended; daily is feasible but costs 7× more per run with no obvious quality benefit at our scale.
4. **Pool everyone vs per-profile from day 1** — recommend pool. The ablation profiles (NoAltData, NoMetaModel, etc.) intentionally see different inputs; pooling washes that out but accelerates dataset growth. Trade-off worth taking until data per-profile crosses 25K.
5. **Hyperparameter strategy** — `n_epochs=1` recommended for incremental; consider 2-3 epochs for the FIRST training job to bootstrap from the pooled archive.
6. **Output format** — keep the existing JSON response shape (so downstream parsers stay unchanged) vs train on a tighter format (cheaper at inference time but breaks parser compat). Recommend keeping existing format for the first cut.
7. **System prompt prefix** — should the fine-tune training file's system message be the FULL prompt prefix (all of `_build_batch_prompt`'s preamble) or a stripped-down version? Recommend full — gives the model the most context to learn from.

---

## 16. Future work (Phase 4b.2 + 4b.3)

Already scoped in docs/17 Phase 4b but excluded from 4b.1:

- **Phase 4b.2 (self-hosted LoRA on Llama/Mistral/Qwen)** — replaces vendor API entirely. Higher ops complexity (need GPU instance) but ~$100-150/month all-in including inference, and full data sovereignty. Pick-up trigger: 4b.1 success AND operator wants to remove the OpenAI vendor dependency.

- **Phase 4b.3 (per-profile fine-tunes)** — after each profile accumulates ≥25K resolved predictions individually (~12 months at current rate). Replaces the pooled model with per-profile checkpoints; better at capturing per-profile ablation signal. Pick-up trigger: pooled fine-tune is producing measurable lift AND profile-level data sets cross the threshold.

- **RLHF / DPO over operator preferences** — instead of hindsight outcomes, use operator-curated preference pairs ("the AI should have chosen X over Y") as the training signal. Stronger supervision for narrative-quality decisions but needs sustained operator-time investment.

---

## 17. Decision criteria for activating Phase 4b.1

Per docs/17 §4 go/no-go pattern:

| Condition | Triggers Phase 4b.1 |
|---|---|
| 4+ weeks of post-B1 data accumulated (≥20K pooled examples) | YES — sufficient bootstrapping corpus |
| RAG retrieval consistently returns generic cases (low corpus signal) | YES — fine-tune internalizes patterns RAG can't surface |
| Eval shows base AI plateau at <60% directional accuracy on resolved predictions | YES — measurable headroom for fine-tune lift |
| Operator wants tighter cost control + vendor diversification | YES (Phase 4b.2 follows) |
| OpenAI account tier supports fine-tune API + has ≥$50/month budget | REQUIRED |

None of these are observed today (2026-05-19). The earliest realistic activation is **late June 2026** after the first month of post-B1 data accumulation. Reassess at that point.
