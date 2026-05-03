# Cost & Quality Levers — Implementation Plan

**Created:** 2026-04-27 (markets closed; weekend session)
**Owner:** Mack + assistant
**Driver:** Daily AI cost trending above $3 ceiling on heavy-deploy days.
Three levers identified that reduce cost AND improve decisions.

---

## Why now

Three cost-reduction levers identified during a token-cost audit
where the user noticed elevated AI spend ($3.54 vs ~$1.32 baseline).
Each lever is independently shippable, has well-bounded scope, and
two of them improve decision quality on top of saving cost.
Markets are closed — the right time to land all three before the
next trading session.

---

## Lever 1 — Persistent disk cache for ensemble + political_context

### Problem

`trade_pipeline._ensemble_cache` and `trade_pipeline._political_cache`
are module-level Python dicts. Every scheduler restart wipes them.
30-min cache means cycles within the same window share results, but
a deploy at 13:45 forces a fresh ensemble fire at 13:46 even though
the cached value was valid until 14:00.

Today's 16-deploy cadence multiplied ensemble fires from baseline ~7
to ~21 per market_type per day. ~$0.50 wasted on this artifact.

### Fix

Move both caches to a SQLite table in `quantopsai.db`:

```sql
CREATE TABLE shared_ai_cache (
    cache_key   TEXT NOT NULL,
    cache_kind  TEXT NOT NULL,   -- 'ensemble' | 'political'
    payload     BLOB NOT NULL,   -- pickle.dumps(...)
    bucket      INTEGER NOT NULL,-- int(time/1800)
    fetched_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (cache_key, cache_kind)
);
```

Read path: `_get_shared_ensemble` / `_get_shared_political_context`
check the table for a row with matching `cache_kind` + `cache_key` +
current bucket. If found, return the unpickled payload. If not, run
the original API call, then write back.

Restart safety: the SQLite row survives any process restart. The
30-min `bucket` math still ensures stale values get evicted.

### Risks & mitigations

- **Pickle compatibility on schema drift.** Mitigation: try/except
  the unpickle; on failure treat as cache miss and refetch.
- **Stale data after a manual data-source change.** Mitigation: same
  30-min TTL still applies; values refresh naturally.
- **Race between two scheduler processes.** Mitigation: SQLite UPSERT
  (`INSERT OR REPLACE`) is atomic; worst case both processes call
  the API once and the second write wins.

### Anti-regression tests

1. End-to-end: seed a row in `shared_ai_cache` with current bucket;
   call `_get_shared_ensemble`; assert API not invoked, value matches.
2. Bucket flip: row with `bucket = current - 1` is treated as miss.
3. Pickle failure: corrupt row triggers refetch, doesn't crash.
4. Cross-process: write from one connection, read from another.
5. Source-level: `_get_shared_ensemble` source must reference the
   shared_ai_cache table read path (regression guard).

### Cost impact

- Deploy-heavy days: saves ~$0.50/day.
- Quiet days: saves ~$0.05/day (just the cycle-boundary edge cases).
- Quality: identical (same payload either way).

---

## Lever 2 — Meta-model pre-gate before ensemble

### Problem

Current pipeline order in `trade_pipeline.py`:

```
shortlist (13-15) → ensemble runs on ALL → batch_select on all 13-15
  → meta-model re-weights/suppresses
```

So the ensemble's 4 specialist calls analyze candidates that the
meta-model later kills. Two costs:

1. ~50% of specialist verdicts are spent on candidates that get
   suppressed downstream.
2. The final `batch_select` AI call sees the full 13-15 list and
   spreads its attention across them all.

### Fix

Insert a meta-model pre-gate step BEFORE the ensemble:

```
shortlist (13-15) → meta-model pre-gate (drops meta_prob < 0.5)
  → ensemble runs on ~7 → batch_select on ~7
  → meta-model re-weights/suppresses survivors
```

When the meta-model isn't trained yet (insufficient data), the
gate falls open — all candidates pass through. So existing behavior
is preserved during data accumulation; the gate only takes effect
once we have a real model.

### Implementation surface

- New helper `_pre_gate_with_meta_model(candidates, ctx)` in
  `trade_pipeline.py`. Loads the per-profile meta-model bundle, runs
  `predict_probability` on each candidate's features, returns
  filtered list.
- Threshold: `meta_prob >= 0.5` to survive the gate. Sub-0.5 means
  the meta-model is more confident the AI is wrong than right.
- Wired into the pipeline immediately AFTER candidate ranking
  and BEFORE `_get_shared_ensemble`.
- Configurable per-profile: `meta_pregate_threshold` (default 0.5).
  Off (=0.0) preserves prior behavior.

### Quality mechanisms (from the discussion)

1. **Specialist attention is finite** — fewer-better candidates
   means each specialist's relative confidence spread is sharper.
2. **batch_select token budget** is bounded — fewer candidates in
   the prompt means more reasoning per remaining candidate.
3. **risk_assessor VETO authority** is reserved for edge cases,
   not wasted on already-doomed candidates.
4. **Calibration data accumulates faster** — ~40% of specialist
   verdicts now get a labeled outcome (vs ~20%), so the Platt
   scaler learns 2x faster.

### Risks & mitigations

- **Meta-model isn't trained yet → gate becomes 0%-pass and blocks
  all trades.** Mitigation: explicit "no model loaded" path returns
  all candidates unchanged.
- **Meta-model is over-confident on rejection (false suppression
  of winners).** Mitigation: 0.5 threshold is conservative. Future
  refinement: track gate "would-have-been-correct" rate by feeding
  rejected candidates' subsequent-day prices back through the gate
  model and flagging when reject decisions are wrong > 30% of the
  time.
- **Ordering bug: pre-gate runs before features are fully built.**
  Mitigation: ensure `feature_payload` is constructed BEFORE the
  pre-gate, then re-used downstream. Tests verify candidates that
  survive the gate carry the same features they would have had
  without gating.

### Anti-regression tests

1. No-model path: `predict_probability` returns None → gate passes
   all candidates through (current behavior preserved).
2. Threshold semantics: candidates with `meta_prob >= 0.5` survive,
   `< 0.5` are dropped.
3. Configurable threshold: profile with `meta_pregate_threshold=0.0`
   preserves prior behavior end-to-end.
4. Feature preservation: candidates that survive gate have identical
   feature payloads to pre-gate equivalents.
5. Source-level: `trade_pipeline.process_candidates` (or whatever
   the entry point is named) must call `_pre_gate_with_meta_model`
   BEFORE `_get_shared_ensemble`.
6. Behavioral: seed a profile with meta-model that returns
   meta_prob=0.4 for symbol X and 0.7 for symbol Y. Run pipeline.
   Assert ensemble was called for Y but NOT X.

### Cost impact

- Saves ~$0.30-0.40/day system-wide once meta-models are trained.
- Quality: improves (4 mechanisms above). Win-rate signal will be
  measurable within 7-14 trading days.

---

## Lever 3 — Per-profile specialist disable list

### Problem

Today's calibration backfill produced calibrators showing
`pattern_recognizer` is **inversely calibrated** on Mid/Small/Small-
Shorts profiles (raw 90 → cal 28). The Platt-scaling layer attenuates
its vote weight, but cannot flip the sign — its BUY at raw 90 still
contributes weakly POSITIVE to buy_score even when its empirical hit
rate is 28%.

### Fix

Add a per-profile config field: `disabled_specialists` (JSON-encoded
list of specialist names). The ensemble:

1. Skips the API call entirely for any specialist in the list (cost
   saving — those specialists' calls are eliminated).
2. Records ABSTAIN(0) in the per-symbol verdict slot so synthesizer
   ignores them (no positive contribution to buy/sell scores).
3. Excludes them from the `ensemble_summary` string passed to the
   final AI prompt (no narrative pollution).

### Auto-(re-)enable mechanism

Daily scheduler task `_task_specialist_health_check`:

- For each profile + specialist combination, check the calibrator's
  current shape. If the calibrator was previously DISABLED but now
  shows positive slope (raw 90 → cal > 50), automatically re-enable.
- Conversely, if a previously-enabled specialist's calibrator goes
  inverse for ≥ 30 consecutive days (raw 90 → cal < 35), automatically
  add it to `disabled_specialists`.

This makes the system self-correcting: dropped specialists can earn
their slot back if their signal recovers; over-confident specialists
get muted automatically without human oversight.

### Quality mechanisms (from the discussion)

1. **Sign-flip beyond what calibration can do** — anti-correlated
   specialist's BUY contribution is removed entirely, not just damped.
2. **Cleaner synthesizer math** — buy_score / sell_score reflect
   only specialists with calibrated edge.
3. **Final AI prompt narrative is cleaner** — `ensemble_summary`
   doesn't show noisy specialist verdicts.
4. **Coverage analysis becomes legible** — once pattern_recognizer
   is dropped, future calibrators on the other 3 specialists train
   on cleaner inputs (their signal isn't being mixed with noise).
5. **risk_assessor VETO becomes higher-information** — its veto
   is no longer offset by noise from another specialist.

### Risks & mitigations

- **Auto-disable fires prematurely on a specialist that briefly
  underperformed.** Mitigation: 30-day threshold + minimum-samples
  gate (need ≥ 50 calibration data points before auto-disable).
- **Disabling all specialists collapses the ensemble to nothing.**
  Mitigation: hard floor — never disable more than 2 of 4 specialists
  per profile.
- **Specialist removed from ensemble but still receives outcomes
  via `update_outcomes_on_resolve`.** Mitigation: outcome recording
  is keyed on `(prediction_id, specialist_name)`. If specialist
  didn't contribute, no outcome row exists, so no calibration data
  accumulates. That's correct behavior — we shouldn't be training
  calibrators on data we didn't use.

### Anti-regression tests

1. Profile with `disabled_specialists=["pattern_recognizer"]` runs
   ensemble; assert pattern_recognizer's API is NOT called.
2. Disabled specialist appears as ABSTAIN(0) in per-symbol verdict.
3. Disabled specialist NOT in `ensemble_summary` string.
4. Auto-disable fires when calibrator slope is inverse for 30 days.
5. Auto-disable BLOCKED when only 2 specialists remain (floor).
6. Auto-re-enable fires when calibrator recovers to positive slope.

### Cost impact

- Saves ~$0.15-0.25/day per profile where a specialist is disabled.
- For pattern_recognizer disabled on Mid/Small/Small-Shorts: ~$0.40/day
  combined.
- Quality: improves (5 mechanisms above).

---

## Order of execution

1. **Lever 1 first** — foundation, no decision-quality change, lowest
   risk, well-scoped (~30-45 min). Ships persistent cache to disk
   and validates the testing pattern.
2. **Lever 3 second** — per-profile config + ensemble filter logic +
   auto-disable scheduler task (~60-90 min). No dependencies on
   Lever 2.
3. **Lever 2 third** — meta-model pre-gate + pipeline reorder + test
   coverage (~90-120 min). Most invasive change because it reorders
   the trade pipeline itself.

Each lever's deploy is independent. If any breaks something, roll
back just that one without affecting the others.

---

## Cumulative impact estimate

| Lever | Daily savings | Decision quality |
|---|---|---|
| 1 — persistent cache | ~$0.50 deploy-heavy / $0.05 quiet | unchanged |
| 2 — meta-model pre-gate | ~$0.30-0.40 (once meta-models train) | better (4 mechanisms) |
| 3 — disable anti-calibrated specialists | ~$0.40 (once auto-disable fires) | better (5 mechanisms) |
| **Total** | **~$1.20-1.30/day** | **better** |

Projected post-deploy daily cost on a normal trading day:
**$1.50-$2.00** (vs today's $3.54, vs Friday's ~$0.42/profile baseline).
Well below the $3 ceiling.

Decision-quality gains will be measurable within 1-3 weeks via:
- Win rate per profile (expect Mid/Small to climb)
- Meta-model AUC per profile (expect calibration data 2x faster)
- Ensemble verdict-vs-outcome alignment per specialist
