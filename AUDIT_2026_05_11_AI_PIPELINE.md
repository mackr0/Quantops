# AI Pipeline Option-Handling Audit — 2026-05-11

**Scope**: Seven-stage pipeline audit for option-vs-stock conflation
bugs.
**Methodology**: Read-only static analysis; focus on data flow + decision
trees.
**Baseline**: Position class refactor (Phase 1-5, commits e55f265 →
70c34de) fixed 6 symbol/OCC bugs in the position-tracking layer. This
audit looks for the SAME class of issue in the layers above.

**Trigger**: TODO #4b. Mack: "options were bolted on to a stock-first
system — the symbol-vs-OCC overload may not be the only symptom."

Findings classified as:
- **BUG** — option treated as stock; outcome is wrong.
- **REUSE_OK** — option uses stock code path because the path is
  genuinely instrument-agnostic.
- **INCOMPLETE** — option path exists but doesn't cover all option
  semantics (gaps).

---

## Critical Findings (Ranked by Impact)

1. **Slippage stats display bloat** (already TODO #8). `metrics.py:634`
   and `journal.get_slippage_stats:1382` use `slippage_pct * price * qty`
   for total cost. For options with penny premiums, slippage % is
   10,000%+ on normal mark-to-market moves, making aggregate values
   impossible (1130% observed). **Critical** — dashboard metrics
   unusable; triggers false risk warnings.

2. **`return_pct` collation in ai_tracker** (`ai_tracker.py:355`):
   identical formula for stocks (`(150-153)/150 = 2%`) and options
   (`(0.50-2.00)/0.50 = 300%`). Stored unscaled in `actual_return_pct`,
   read by self_tuning. **High** — option moves dominate aggregate
   stats; tuner sees option signal as if it were stock.

3. **Self-tuning parameter corruption** (`self_tuning.py:62-73`,
   342-356). Win-rate aggregates mix stock BUY (2% move = win) with
   option SHORT (20%+ move = win). Tuner adjusts STOCK parameters
   (`stop_loss_pct`, `max_position_pct`) based on option-dominated
   outcomes. **High** — distorts stock parameters whenever option
   activity spikes.

4. **AI prompt feature parity asymmetry** (`ai_analyst.py:75-92`).
   Option candidates see only stock technicals (RSI, MACD, Bollinger).
   ZERO option-specific features: IV rank, Greeks, days-to-expiry,
   spread max-loss/max-gain, bid-ask spread on the contract. **High** —
   AI assesses option strikes blind to option fundamentals.

5. **Multileg trades bypass specialist risk veto**
   (`ai_analyst.py:315-319` → `options_multileg.execute_multileg_strategy`).
   Stock BUY/SELL routes through specialist ensemble (each can VETO);
   MULTILEG_OPEN goes direct to executor. **High** — wide-loss spreads
   and Greeks mismatches cannot be blocked by the risk_assessor or
   adversarial_reviewer specialists.

---

## Detailed Findings by Stage

### Stage 1 — AI Prompt Construction (`ai_analyst.py`)

| Line | Finding | Class |
|---|---|---|
| 75-92 | `tech_summary` dict feeds Claude RSI/MACD/Bollinger/SMA/volume — stock-only indicators. No IV rank, Greeks, DTE, premium level, bid-ask. | **INCOMPLETE** |
| 315-319 | `render_multileg_recs_for_prompt()` adds multileg block AFTER stock-focused frame, not integrated. | **REUSE_OK** but option context bolted on. |
| 193-200 | `analyze_symbol_consensus()` — no special handling for OCC vs underlying. | **REUSE_OK** (price-movement analysis is instrument-agnostic). |

### Stage 2 — Strategy Signals (`strategies/*.py`)

| Files | Finding | Class |
|---|---|---|
| `iv_regime_short.py`, `vol_regime.py`, `high_iv_rank_fade.py`, `max_pain_pinning.py` | Read IV/GEX, but emit stock-symbol BUY/SELL. Multileg wrapping happens downstream in advisor. | **REUSE_OK** (options-aware in features), but: |
| `ai_analyst.py:315-319` | Multileg wrapping is reactive — after stock candidate screening — not part of strategy signal creation. Multileg setups don't influence which symbols enter the funnel. | **INCOMPLETE** |

### Stage 3 — AI Prediction Tracker (`ai_tracker.py`)

| Line | Finding | Class |
|---|---|---|
| 355 | `return_pct = ((current_price - pred_price) / pred_price) * 100.0` — identical for stocks (2% range) and options (50-200% range). | **BUG** |
| 594 | `DIRECTIONAL_SQL` includes `'MULTILEG_OPEN'` in directional bucket. Multileg P&L should be tracked separately (defined-risk, bounded outcomes). | **BUG** |
| 472-478 | `actual_return_pct` stored without option/stock distinction. Self-tuning later reads it. | **BUG** |

### Stage 4 — Metrics Layer (`metrics.py`, `journal.py`, `mfe_capture.py`)

| Location | Finding | Class |
|---|---|---|
| `metrics.py:634` | `slippage_impact += abs(slip / 100 * price * qty)` — for stocks $2; for options 100x bigger. | **BUG** (Critical) |
| `journal.py:1377-1392` | `get_slippage_stats()` aggregates option legs with penny entry premiums → balloons `avg_slippage_pct` to 1130%. | **BUG** (Critical) |
| `mfe_capture.py:98-102` | `notional = abs(sell_qty * entry_price)` — for options, premium-dollars not underlying notional. | **INCOMPLETE** |
| `metrics.py:1304-1322` | Turnover uses `qty` directly. 100 1-lot calls = 10,000 share notional, counted as 100. Short legs may be incorrectly signed. | **BUG** |

### Stage 5 — Self-Tuning (`self_tuning.py`)

| Line | Finding | Class |
|---|---|---|
| 62-73 | `_get_current_win_rate()` aggregates stock + option predictions; option moves dominate. | **BUG** |
| 342-356 | `get_performance_breakdown()` groups by `predicted_signal` — MULTILEG_OPEN diluted with BUY/SELL. | **BUG** |
| 449-461 | Short-selling win-rate threshold doesn't distinguish short stock vs short calls (different risk profiles). | **INCOMPLETE** |
| 590-650 | Tuning history adjusts parameters based on win-rate changes — no option/stock weighting. | **BUG** |

### Stage 6 — Specialists (`specialists/*.py`)

| Location | Finding | Class |
|---|---|---|
| `specialists/_common.py:102` | `candidates_block()` is instrument-agnostic — fine in itself. | **REUSE_OK** |
| `ai_analyst.py:315` | Multileg trades route to `options_multileg.execute_multileg_strategy()` directly, NOT through specialist ensemble. Specialists cannot VETO a wide-loss spread or Greeks mismatch. | **BUG** |
| `risk_assessor.py` | No option-specific logic — assumes stock risk metrics apply. No mention of Greeks or time decay. | **INCOMPLETE** |
| (system-wide) | No option-specific specialists exist (IV-skew specialist, spread-P&L specialist). | **INCOMPLETE** |

### Stage 7 — Risk Model (`portfolio_risk_model.py`)

| Line | Finding | Class |
|---|---|---|
| 603-606 | `compute_portfolio_risk_from_positions()` reads `market_value` per position — correct for capital-at-risk. | **REUSE_OK** |
| 608-623 | For a long 100 calls position, fetches UNDERLYING ticker bars and regresses 1:1. Should be delta-weighted: long $5 call on $200 stock = $500 capital but ~$200 underlying-direction exposure (delta-adjusted). | **BUG / INCOMPLETE** |
| 654-675 | `render_risk_summary_for_prompt()` displays factor exposures but no Greek aggregation (delta sum, gamma, vega, theta). AI prompt blind to time decay and vol-of-vol risk. | **INCOMPLETE** |

---

## Structural Root-Cause Diagnosis

The option-handling bugs recur because of three architectural decisions
baked early in the system's life:

### 1. Instrument parity at the data layer, NOT the feature layer

The Position class refactor (May 11) disambiguates `symbol` vs
`occ_symbol` at READ time. ✓ Good. But the feature pipeline
(`ai_analyst.py`, strategies, metrics, tuning) still uses the same
template for stocks and options.

**Why**: Options were bolted on AFTER stock infrastructure was mature.
Rather than fork the prompt, metrics, and tuning logic, the team added
option *actions* (`MULTILEG_OPEN`) and option *data* (IV rank, Greeks)
ALONGSIDE stock data, not INSTEAD OF it.

**Result**: Both instruments pass through identical processing; stocks
and options' 10×-scale differences cause aggregation bugs.

### 2. Return % as a universal outcome metric

`ai_tracker.py` stores `actual_return_pct` as the single outcome label.
For stocks, a 2% move takes 5+ trading days; for options, 100% moves
happen in hours.

`self_tuning.py` reads `actual_return_pct` to assess AI quality and
tune parameters. Options' faster/larger moves bias the tuner toward
option-friendly settings.

**Why**: Return % is simple and works for stocks. Extending to options
without scaling (e.g., notional-adjusted return %) was cheaper than
forking.

**Result**: Prediction accuracy metrics and parameter tuning are
distorted whenever option activity spikes.

### 3. Multileg bypass of specialist risk checks

In `ai_analyst.py:315-319`, when the AI proposes `MULTILEG_OPEN`, it
goes DIRECTLY to `options_multileg.execute_multileg_strategy()` without
passing through the specialist ensemble.

Stock trades (BUY/SELL) route through specialists; each specialist
votes, and veto-authorized specialists can block.

**Why**: Multileg was added late; the prompt-generation code just
checks "is multileg_block non-empty?" and adds the action. Specialist
routing was not retrofitted.

**Result**: Multileg trades have no risk check; a specialist cannot
VETO a wide-loss spread or a Greeks mismatch.

---

## Action Priority

### Immediate (blocks confidence in metrics)

1. **Fix slippage % calculation** (TODO #8) — gate on option premium
   floor OR report in dollars-only for options OR split panel.
2. **Fix `actual_return_pct` scaling** — store option-scaled separately
   or notional-weight before aggregation.

### High (corrupts tuning + risk model)

3. **Fork `ai_predictions` win-rate logic** — separate option bucket
   from stock bucket; tune each independently.
4. **Delta-adjusted exposure in risk model** — `position.delta *
   contracts → underlying notional`, not 1:1 `market_value`.

### Medium (UX + governance)

5. **Route `MULTILEG_OPEN` through specialist veto** before execution.
6. **Enrich option prompt** with IV/Greeks/DTE; split `ai_analyst`
   into stock-path and option-path at the feature level.

### Low (polish)

7. Add option-specific specialists (IV-skew, spread-P&L).
8. Greek aggregation in risk-summary prompt.

---

## Status

| TODO # | Item | Status |
|---|---|---|
| #8 | Slippage 1130% bug — Item 1 above | OPEN (already in TODO.md) |
| #9 (new) | Option/stock return_pct scaling — Items 2+3 | TO ADD |
| #10 (new) | Delta-adjusted exposure in risk model — Item 4 | TO ADD |
| #11 (new) | Specialist veto on multileg trades — Item 5 | TO ADD |
| #12 (new) | Option-aware AI prompt features — Item 6 | TO ADD |

(Items 7-8 from priority list are P3 polish — not adding to TODO until
P0/P1/P2 cleared.)
