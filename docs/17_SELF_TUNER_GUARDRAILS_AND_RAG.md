# Self-Tuner Guardrails + RAG over Resolved Trades

Plan for closing two known weaknesses in the learning loop:

1. **Self-tuner over-restriction failure mode** (compounded tightening → trade-rate collapse)
2. **LLM doesn't learn from outcomes** (Claude's weights are frozen — we need an in-context retrieval workaround)

## Why this matters

Per `feedback_self_tuner_must_drift_toward_trading`: default bias must be LOOSEN, restrictions need auto-expiry, "rescue scripts" indicate architectural failure. The 2026-05-14 incident (`project_self_tuner_overcorrection_2026_05_14`) showed 14 days of compounding tightening killed stock entries entirely.

Per the deep-system analysis 2026-05-18 PM: the LLM portion of the pipeline doesn't learn. Only the calibration layers (per-symbol track record, meta-model, self-tuner, learned_patterns) compound over time. The biggest single unlock for "the LLM gets smarter" is **in-context retrieval over its own resolved trades** — the AI sees specific relevant cases on each decision rather than relying on a frozen training cutoff.

## Build order

### Phase 1 — Self-tuner guardrails (defensive: close the over-restriction failure mode)

| # | Guardrail | What it prevents | Status |
|---|---|---|---|
| 1 | **Per-cycle delta cap** | Single cycle can't tighten any parameter by more than X% | **Landed 2026-05-18** — `_apply_param_change` wrapper in `self_tuning.py:136` with ±25% per-cycle cap (`_MAX_PCT_PER_CYCLE`). All ~30 numeric-parameter optimizer call sites routed through it. Helpers `_clamp_delta` and `_within_reference_window` ready for item #3 once day-1 reference persistence is added. |
| 2 | **Trade-count floor with auto-loosen** | If trade count drops below N over 7 days, the most-restrictive parameter is FORCED to loosen by Y% | Pending |
| 3 | **Reference window invariant** | No parameter can drift more than ±50% from its day-1 value without operator override | Pending wiring (helper `_within_reference_window` built; needs persistence of day-1 reference per profile) |
| 4 | **Auto-expiry on restrictions** | Every tightening has a TTL (default 14 days). After TTL it auto-reverts unless re-justified by recent loss evidence | Pending |
| 5 | **Trade-rate anomaly alert** | If weekly trade count drops >50%, fire `/issues` alert and pause self-tuner pending review | Pending |

Each is a small deterministic check added to the existing `self_tuning.py` decision rules. Order chosen to maximize early payoff:
- #1 stops the cascade *directly* (single biggest fix)
- #2 encodes "drift toward trading" as a *hard rule*, not a hope
- #3 is the safety belt on top of #1+#2
- #4 cleans up the accumulation of stale restrictions
- #5 gives the operator visibility when something is off

### Phase 2 — RAG over resolved trades / post-mortems

Pre-decision case-file injection into the AI prompt. The LLM doesn't learn weights but it sees specific relevant past cases on every call — effectively few-shot learning over the system's own history.

| Component | Approach |
|---|---|
| **Embedding generation** | At trade-resolve time, compute an embedding over `(symbol, signal_type, market_context, regime, outcome)` and persist it on the `ai_predictions` row |
| **Retrieval at decision time** | For each new candidate, retrieve top-N most-similar past resolved trades from the SAME profile (then optionally cross-profile if same `strategy_type`) |
| **Prompt injection** | Inject the retrieved case files into the system prompt as "here's what happened last time you faced similar setups" |
| **Embedding backend** | Use Anthropic's embedding endpoint (matches our LLM provider) OR a local Sentence-BERT model (no external call) — TBD on cost / latency tradeoff |

### Phase 3 (deferred but committed to) — Specialist library expansion: 8 → 200

**Current state**: 8 specialists in the ensemble. Per the 2026-05-17 #175 commit (`specialists/_common.py` per-specialist alt-data routing), the 8 active ones are:

| # | Specialist | Role |
|---|---|---|
| 1 | `pattern_recognizer` | Technical patterns: breakouts, squeezes, support/resistance |
| 2 | `risk_assessor` | Fundamentals + short interest + risk-factor diff + EPA/OSHA + FDA + NHTSA + macro |
| 3 | `sentiment_narrative` | Insider + Congress + StockTwits + Google Trends + Wikipedia + activists |
| 4 | `adversarial_reviewer` | Devil's advocate — looks for reasons the trade should NOT happen |
| 5 | `iv_skew_specialist` | Options IV skew analysis |
| 6 | `gamma_pin_specialist` | Gamma pinning + intraday options effects |
| 7 | `option_spread_risk` | Multi-leg spread Greeks + max-loss bounding |
| 8 | (placeholder for the 5th stock-pipeline specialist) | TBD |

**Target state**: 200 specialists.

**Why 200 not 50**: quant funds typically run libraries of 100-300 deterministic signal/veto checkers. Each one captures a narrow pattern (e.g., "if RSI > 80 AND volume > 3× avg AND insider sold in last 30 days, veto LONG"). They're cheap to run (pure code, no API calls), easy to A/B test, and the library compounds — once written, a specialist works forever (assuming the signal it captures is real). 200 gives enough coverage of failure modes that the AI's narrative-reasoning layer rarely flies blind.

**Growth path**:

| Phase | Specialist count | Source of new specialists |
|---|---|---|
| Today | 8 | Initial build |
| Month 1 | 15-20 | Patterns surfacing from first month's resolved trades (each significant losing pattern → 1 specialist) |
| Month 3 | 40-60 | Add specialists for: each major sector regime, each event-type (earnings, M&A, restatements), each volatility regime, each macro context |
| Month 6 | 100-120 | Cross-asset specialists (bond yields → equity sectors), seasonality, cross-listing arbitrage, options-vs-equity divergence, etc. |
| Year 1 | 150-200 | Pattern library complete; ongoing maintenance + decay-replacement |

**Cadence**: ~1 specialist per day of focused work, but realistically batched: 5-10 specialists per week as patterns from resolved-trade post-mortems accumulate. Significant losing-trade patterns are the primary feed — each post-mortem that identifies a recurring trap becomes a candidate specialist.

**Operational consequence**: as the library grows, the AI's role shifts from "decider" to "tie-breaker." With 200 specialists, most candidates will be unambiguous (clear majority pattern). The LLM only resolves the genuinely-contested cases. Cost per cycle DROPS because most decisions short-circuit before the LLM call.

### Phase 4 (deferred) — Prompt engineering, fine-tune, quant-ML

Documented in the deep-system analysis. Not part of this build pass.

## Test plan per phase

- Phase 1 fixes: unit tests for the cap / auto-loosen / reference-window logic against synthetic tuning_history sequences; one integration test that simulates 14 days of compounding tightening and asserts the cascade is broken
- Phase 2 RAG: unit test that retrieval returns top-N matches; one integration test that AI prompt includes case files; before/after measurement of decision quality is a longer-horizon evaluation, not a deploy-time test

## Deploy + ops

- Every commit: `./sync.sh` + full test suite (3794+ passing)
- After phase 1 lands: monitor `tuning_history` table for restriction events; verify auto-loosen fires when synthetic conditions are met
- After phase 2 lands: monitor `cycle_data_*.json` shortlist entries for the new case-file field
