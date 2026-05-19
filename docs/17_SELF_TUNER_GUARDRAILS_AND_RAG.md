# Self-Tuner Guardrails + RAG over Resolved Trades

Plan for closing two known weaknesses in the learning loop:

1. **Self-tuner over-restriction failure mode** (compounded tightening → trade-rate collapse)
2. **LLM doesn't learn from outcomes** (Claude's weights are frozen — we need an in-context retrieval workaround)

## Why this matters

Per `feedback_self_tuner_must_drift_toward_trading`: default bias must be LOOSEN, restrictions need auto-expiry, "rescue scripts" indicate architectural failure. The 2026-05-14 incident (`project_self_tuner_overcorrection_2026_05_14`) showed 14 days of compounding tightening killed stock entries entirely.

Per the deep-system analysis 2026-05-18 PM: the LLM portion of the pipeline doesn't learn. Only the calibration layers (per-symbol track record, meta-model, self-tuner, learned_patterns) compound over time. The biggest single unlock for "the LLM gets smarter" is **in-context retrieval over its own resolved trades** — the AI sees specific relevant cases on each decision rather than relying on a frozen training cutoff.

## Build order

### Phase 1 — Self-tuner guardrails (defensive: close the over-restriction failure mode) — **COMPLETE 2026-05-18**

All five layers shipped in a single day (2026-05-18) atop the existing `tuning_auto_expiry.py` infrastructure. The four autonomous layers (1, 2, 3, 4) prevent and unwind over-restriction structurally; the fifth (5) surfaces the symptom so the operator knows when the autonomous systems are actively working. The tuner is never paused — remediation is entirely deterministic per `feedback_ai_driven_no_manual_loop`.

| # | Guardrail | What it prevents | Status |
|---|---|---|---|
| 1 | **Per-cycle delta cap** | Single cycle can't tighten any parameter by more than X% | **Landed 2026-05-18** — `_apply_param_change` wrapper in `self_tuning.py:136` with ±25% per-cycle cap (`_MAX_PCT_PER_CYCLE`). All ~30 numeric-parameter optimizer call sites routed through it. Helpers `_clamp_delta` and `_within_reference_window` ready for item #3 once day-1 reference persistence is added. |
| 2 | **Trade-count floor with auto-loosen** | If trade count drops below N over 7 days, the most-restrictive parameter is FORCED to loosen by Y% | **Landed 2026-05-18** — `_optimize_trade_count_auto_loosen` in `self_tuning.py`. Trigger: `<3` stock entries in last 7 days. Action: picks the entry-filter parameter with the highest restriction score from PARAM_BOUNDS, loosens it 25% (matches the Item 1 cap so it passes without further clamping), routes through `_apply_param_change` so the change appears in `tuning_history`. Tagged LOOSEN — fires FIRST in the registry. 24 new tests. |
| 3 | **Reference window invariant** | No parameter can drift more than ±50% from its day-1 value without operator override | **Landed 2026-05-18** — `param_references` table + `get_param_reference` / `record_param_reference_if_absent` / `clear_param_references` helpers in `models.py`. `_apply_param_change` now records `old_value` as the day-1 reference on first observation and consults it via the existing `_within_reference_window` helper. Both `full_reset_2026_05_18.py` and `clean_orphaned_profiles.py` wired to wipe references. 17 new tests including the original 14-cycle cascade scenario (stops at 0.05 floor vs 0.00178 without). |
| 4 | **Auto-expiry on restrictions** | Every tightening has a TTL (default 14 days). After TTL it auto-reverts unless re-justified by recent loss evidence | **Landed 2026-05-18** — `expired_at` column added to `tuning_history`; `get_expirable_tightenings` + `mark_tuning_event_expired` helpers in `models.py`; `_optimize_auto_expire_old_tightenings` in `self_tuning.py` (tagged LOOSEN). Picks the oldest unexpired tightening >14d old whose outcome isn't 'improved' and walks the parameter one cap-bounded step back toward the pre-tightening value. Marks the row expired once the value reaches the target. 28 new tests. |
| 5 | **Trade-rate anomaly alert** | If weekly trade count drops >50%, fire `/issues` alert (observability only — the tuner is NOT paused, per `feedback_ai_driven_no_manual_loop`) | **Landed 2026-05-18** — new `trade_rate_anomaly.py` module with `detect_anomaly` / `record_alert` / `resolve_alert_if_recovered` / `check_and_alert`. Wired as daily scheduler task `_task_trade_rate_anomaly_check` in `multi_scheduler.py`. Writes a stable per-profile-per-prior-week signature into the existing `audit_alerts` table so `/issues` picks it up; resolves automatically when trade rate recovers. Structural test pins that the module never mutates `enable_self_tuning` or calls `update_trading_profile` — pure observability. 17 new tests. |

Each is a small deterministic check added to the existing `self_tuning.py` decision rules. Order chosen to maximize early payoff:
- #1 stops the cascade *directly* (single biggest fix)
- #2 encodes "drift toward trading" as a *hard rule*, not a hope
- #3 is the safety belt on top of #1+#2
- #4 cleans up the accumulation of stale restrictions
- #5 gives the operator visibility when something is off

### Phase 2 — RAG over resolved trades / post-mortems — **COMPLETE 2026-05-18**

Pre-decision case-file injection into the AI prompt. The LLM doesn't learn weights but it sees specific relevant past cases on every call — effectively few-shot learning over the system's own history.

| Component | Approach (as shipped) |
|---|---|
| **Embedding generation** | Derived ON DEMAND from existing `ai_predictions` columns (`symbol`, `predicted_signal`, `regime_at_prediction`, `strategy_type`, `confidence`, `features_json`, `actual_outcome`, `actual_return_pct`). No schema migration needed; no persisted vectors. Numeric features (RSI, momentum, volume ratio, gap, ATR) bucketed into stable bands so TF-IDF treats them as discrete tokens. |
| **Retrieval at decision time** | `case_file_rag.retrieve_similar` fits TF-IDF on the rolling-window corpus + candidate text (sklearn — already installed; no new deps), returns top-N above a 0.15 cosine-similarity floor. Same-profile only by default. Returns BOTH wins and losses per `feedback_self_tuner_must_drift_toward_trading` (filtering to warnings would bias away from action). |
| **Prompt injection** | `_build_batch_prompt` in `ai_analyst.py` calls `build_prompt_block` per candidate. Outputs a "SIMILAR PAST CASES" block: each line is `[date] SIGNAL SYMBOL in regime → OUTCOME (return in days, sim=X)` plus an indicator-key=value sub-line. Fail-soft — empty corpus or missing DB yields no block, the existing prompt still works. |
| **Embedding backend** | TF-IDF (sklearn). Chosen over sentence-transformers because: (1) case files are highly structured token sequences, not natural language paraphrasing; (2) no PyTorch / no 1GB+ disk cost on the droplet; (3) deterministic + fast (no model load). Can be upgraded to sentence-transformers later if quality measurably lags. |

Implementation: `case_file_rag.py` (270 lines), wired into `ai_analyst.py:_build_batch_prompt`. 22 new tests covering the text builder, retrieval ranking + thresholds, win/loss balance, format rendering, and the prompt-builder integration.

### Phase 3 (IN PROGRESS) — Specialist library expansion: 8 → 200

**2026-05-18 update.** First Phase-3 batch landed. Original framing was "wait for post-mortems to surface patterns" — that's the CALIBRATION mechanism, not the discovery mechanism. Most quant patterns are well-documented in the literature and don't need a losing trade to teach us. Discovery is now aggressive.

**Architecture decision.** The 192 new specialists are NOT LLM-narrative specialists (that would 25× the per-cycle AI cost). They're **deterministic code-only rule checkers** in a new `deterministic_specialists/` directory:

  - Each rule = pure function `(candidate, ctx) → Optional[{severity, reasoning}]`
  - Severities: `VETO` (high-confidence stop), `CAUTION` (yellow flag), `CONFIRM` (pattern supports the signal)
  - Zero per-rule API cost — runs as a panel block injected into the prompt, weighed by the LLM
  - Registered in `RULE_MODULES`; each gated by `APPLIES_TO_SIGNALS` so SHORT rules don't fire on BUY candidates and vice versa

**Status after second batch (2026-05-18, same day):**
- **101 deterministic specialists** in `deterministic_specialists/`
- Plus the **8 LLM-narrative specialists** from `specialists/` = **109 total specialists** in the live ensemble
- Up from 8 at session start. The original "Month 6: 100-120" projection achieved in a single day — because most quant patterns are documented in literature; "wait for losses" was only the *calibration* mechanism, never the *discovery* one.

Categories shipped in the first batch:
- Late-stage / extended pattern warnings (RSI overbought + 52w high, parabolic blow-off, gap-into-resistance, bearish divergence, VWAP extension, MFI overbought, CMF distribution)
- Breakout / momentum quality (volume-dry breakout, low-ATR breakout, weak-ADX breakout)
- Smart-money + crowding (insider sold, high SI, crowded long, StockTwits euphoria, FINRA short vol)
- Smart-money + flow confirms (insider cluster buying, 13D activist, dark-pool accumulation, congressional buying, UOA aligned, StockTwits capitulation)
- Earnings / analyst momentum (EPS up-revisions, down-revisions, beat streak, miss streak, in-window earnings)
- Regulatory / corporate-event (8-K Items 1.03/4.02/2.06, 8-K Item 5.02, risk-factor diff additions, FDA citations, NHTSA recalls, SEC HIGH/CRITICAL alerts)
- Trend / pattern confirms (strong ADX, RSI oversold in uptrend, 3×+ volume confirm, sector RS, sector weakness, sector downtrend long, CMF accumulation, MFI oversold, near Fib support, TTM-squeeze release, ORB breakout)
- Short-side specific (extended below VWAP, high borrow cost, HIGH squeeze risk)
- Macro / volatility regime (IV extreme high, cross-asset vol high, yield curve inverted, CBOE SKEW extreme)
- Execution / friction (high slippage, news cluster without parsed SEC catalyst)

| Phase | Specialist count | Source of new specialists |
|---|---|---|
| Session start (2026-05-18) | 8 | Initial LLM ensemble |
| First batch (2026-05-18) | 60 | Phase 3 framework + 44 deterministic rules |
| Second batch (2026-05-18) | **109** | +49 more rules — trend/momentum, gap, microstructure, attention, smart-money quality, fundamentals (PE), options (IV/PCR), macro (low-vol/skew/curve-steepening), 8-K specifics, calendar/time-of-day |
| Next ~50 target | ~150 | Continue from the literature catalog (factor exposures, candlestick proxies, microstructure events, dividend-cycle effects, ETF flow signals) |
| Year 1 | 150-200 | Mature library with calibrated weights from realized outcomes |

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
