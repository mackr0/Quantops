# Position-cap as soft bound — every cap is an AI-tunable parameter

> **Archived 2026-06-04.** Describes state as of 2026-05-20 PM (SCOPED). Phase 1 (§3.1 pre-filter drop + §3.3 SELL-before-BUY + §3.4 multileg greek gate) and Phase 2 §3.5 (greek caps in Settings UI) have demonstrably landed per the docs/AUDIT_2026_06_04_DOC_RECONCILIATION.md verification. Remaining items (§3.6 three new tuners + max_total_positions LOOSEN direction, §3.7 param_bounds entries) belong on OPEN_ITEMS if they haven't shipped. Design rationale preserved here.

**Caps exist as risk parameters. They are operator-settable in the UI and autonomously adjusted by the self-tuner based on observed outcomes. The AI trades within whatever the current cap is and can self-direct around it (emit SELL on a current holding to free room for a better candidate); it does NOT bypass caps on a per-trade basis. This brings every cap (greek-exposure and position-count) into the same paradigm that already exists for `max_position_pct`, `stop_loss_pct`, `take_profit_pct`, `ai_confidence_threshold`, etc.**

Status: SCOPED 2026-05-20 PM.
Owner: TBD.
Triggered by: operator review of the 2026-05-20 cycle-time + #189 incidents. The pre-filter's "at max_total_positions" check was dropping every new candidate before the AI saw it — directly against `feedback_trade_and_make_money_not_hoard`. Plus #189's fix exposed gaps where greek caps aren't enforced on multileg and aren't tunable like every other parameter.
Depends on: #189 done (committed fe25a18; stock pipeline cleanly separated from option pipeline).

---

## 0. TL;DR

Today: `max_total_positions` blocks the AI from seeing new candidates whenever the count is reached. Greek caps exist on the schema but are partially enforced (single-leg yes; multileg no) and not tunable (no UI, no self-tuner).

After: every cap is in the same paradigm as the rest of the tunables. AI sees all candidates with cap context, can self-direct around caps by emitting SELL+BUY pairs in the same cycle. Self-tuner adjusts caps over time based on outcomes (raises when realized risk under cap consistently good; lowers when caps hit + outcomes bad). Operator override via Settings UI.

No new AI action types. No atomic SWAP protocol. Just: pre-filter stops blocking, cycle execution orders SELLs before BUYs, AI prompt shows current caps, self-tuners exist for every cap.

---

## 1. Operator principles this honors

From `feedback_trade_and_make_money_not_hoard`:

> Position-cap as soft bound (2026-05-20 addendum): Position limits are not hard caps that block AI judgment — they are upper bounds the AI is told about and works around. When a profile is at max_total_positions, the pre-filter must NOT discard new candidates from the screener. The AI should always see them and have the ability to make a swap decision ("the new candidate is better than my weakest current holding — close X to open Y").

> Cash sitting idle after exits is a system failure. The next cycle that runs after any exit must actively deploy the freed cash.

From `feedback_self_tuner_must_drift_toward_trading`:

> Default bias = LOOSEN; restrictions need auto-expiry; rescue scripts = architectural failure

Both apply directly: drop the at-max pre-filter block, ensure cap-tighten is data-driven (not the only direction the tuner moves), make every cap tunable so the AI's space of permissible actions expands with proven track record.

---

## 2. Current state — by parameter

| Param | DB column | Settings UI | Self-tuner | Enforced where | Notes |
|---|---|---|---|---|---|
| `max_position_pct` | ✓ | ✓ | ✓ | `trade_pipeline.py:803-815` (sizing) | already in paradigm |
| `max_total_positions` | ✓ | ✓ | partial (TIGHTEN-only) | `trade_pipeline.py:1437-1447 + 1532-1538` (count + pre-filter) | tuner only goes DOWN; pre-filter blocks candidates from AI |
| `stop_loss_pct` | ✓ | ✓ | ✓ | per-trade entry | in paradigm |
| `take_profit_pct` | ✓ | ✓ | ✓ | per-trade entry | in paradigm |
| `ai_confidence_threshold` | ✓ | ✓ | ✓ | AI veto layer | in paradigm |
| `max_net_options_delta_pct` | ✓ | **✗** | **✗** | `options_trader.py:532` single-leg only | NOT in paradigm; no UI, no tuner, multileg unenforced |
| `max_theta_burn_dollars_per_day` | ✓ | **✗** | **✗** | single-leg only | same |
| `max_short_vega_dollars` | ✓ | **✗** | **✗** | single-leg only | same |

Three caps need to join the paradigm; one needs paradigm completion (tuner direction). One pre-filter behavior needs to change.

---

## 3. Changes by file

### 3.1 `trade_pipeline.py` — pre-filter stops blocking at-max

**Today** (lines 1532-1538):
```python
if at_max_positions and symbol not in held_symbols:
    pre_filter_skips.append({
        "symbol": symbol, "action": "SKIP",
        "reason": "At max positions, can only close existing",
    })
    continue
```

**After:** delete that block. Candidates flow through to the strategy/AI evaluation. AI sees them and decides per the prompt context (see §3.2).

### 3.2 `ai_analyst.py` — AI prompt shows current caps + cash + holdings

Add a context block to the prompt:
```
## Risk budget (you trade within these; the self-tuner adjusts them over time)
- Current positions: 8 of 10 (max_total_positions = 10)
- Available cash: $4,230 (of $25,000 equity)
- Greek exposure (options only):
    Net delta: $1,240 (cap: $1,250 — at 99%)
    Net theta: -$22/day (cap: -$50/day — at 44%)
    Net vega: +$310 (no short-vega exposure)

## Acting near a cap
If a new candidate looks better than a current holding, emit SELL on
the weak holding AND BUY on the new candidate in the same cycle. The
SELL frees cash before the BUY tries to use it (execution orders
SELLs before BUYs within a cycle).

If the cap is too tight for what the market is offering, the
self-tuner will widen it over time based on outcomes — don't
override per-trade.
```

The AI now has explicit information to self-direct. It doesn't need a new SWAP action; it just emits SELL on one candidate and BUY on another in the same cycle's prediction list.

### 3.3 `trade_pipeline.py` — execution orders SELLs before BUYs in same cycle

Where the AI's per-symbol decisions are dispatched (around `_execute_trade` calls in the candidates loop), partition the decisions:
1. First pass: execute all SELL/STRONG_SELL actions on existing positions. Cash credits land in the account.
2. Second pass: execute all BUY/STRONG_BUY/OPTIONS/MULTILEG_OPEN actions. They draw from the now-larger cash balance.

Currently the loop is per-candidate in order — random/cycle order. If a BUY processes before a SELL on the same cycle, the BUY may exceed available cash even though the SELL was about to free it. Reordering closes this gap.

### 3.4 `options_multileg.py` (or `pipelines/option.py`) — wire greek gate

Mirror the single-leg call site at `options_trader.py:497-540` in the multileg execution path. Compute the strategy's aggregate greek contribution, call `check_greeks_gates(book_summary, contribution, ctx)`. If `not allowed`, return `result["action"] = "SKIP"` with `gate_result["reasons"]` joined as the reason. Identical pattern; no new infrastructure.

### 3.5 `templates/settings.html` — add UI for greek caps

Three new number inputs in the Settings form, alongside the existing risk-param inputs:
- `max_net_options_delta_pct` (with help text: "Cap on |options-only delta| / equity. Self-tuner adjusts based on realized risk.")
- `max_theta_burn_dollars_per_day`
- `max_short_vega_dollars`

Mirror the existing pattern (label + numeric input + help tooltip). `views.save_profile` already accepts arbitrary profile columns from the form via the ALLOWLISTED_COLUMNS mechanism — add these three to that allowlist.

### 3.6 `self_tuning.py` — three new tuners + max_total_positions LOOSEN direction

**Three new tuners:**
- `_optimize_max_net_options_delta_pct(conn, ctx, profile_id, ...)`:
  Read N most recent resolved option predictions. Compute realized delta vs cap utilization. If consistently under cap (e.g., max observed utilization <70% over 30+ resolved trades AND no large drawdowns triggered by delta) → raise cap by 1 step (e.g., 5% → 6%, bounded by `param_bounds.py`). If cap was hit AND outcomes negative (avg post-cap-hit drawdown > threshold) → lower cap by 1 step.
- `_optimize_max_theta_burn_dollars_per_day`: same pattern, theta-utilization vs theta-burn-driven losses.
- `_optimize_max_short_vega_dollars`: same pattern, vega exposure vs short-vega-driven losses (e.g., post-VIX-spike drawdowns).

**`max_total_positions` LOOSEN direction:**
Add the symmetric LOOSEN branch to `_optimize_max_total_positions`. Current code (`self_tuning.py:2789-2830`) only computes `new_val = current - 1` (TIGHTEN). Add: if profile has been at cap for N consecutive cycles (e.g., 10) AND average per-position return is positive AND no concentration-driven drawdown event → `new_val = current + 1`. Bounded by `param_bounds.py` (max 25 per current bound).

**Tag updates:** `self_tuning.py:2180` currently tags `_optimize_max_total_positions` as `"TIGHTEN"`. Change to `"BOTH"` (or whatever the tuner's direction enum supports — check the codebase). Same tag for the three new greek-cap tuners.

### 3.7 `param_bounds.py` — bounds for greek caps

Add reasonable bounds so the tuner can't run away:
- `max_net_options_delta_pct`: (0.01, 0.20) — 1%-20% of equity
- `max_theta_burn_dollars_per_day`: (10.0, 500.0) — $10-$500/day
- `max_short_vega_dollars`: (50.0, 5000.0) — $50-$5000

### 3.8 `tests/`

- `test_pre_filter_does_not_block_at_max_2026_05_NN.py`: profile at `max_total_positions`; new candidate enters cycle; assert it reaches the AI step (not dropped by pre-filter).
- `test_execution_orders_sells_before_buys.py`: per-cycle dispatch with mixed SELL+BUY decisions; assert SELLs execute first.
- `test_greek_gate_wired_to_multileg.py`: multileg execution path receives a `check_greeks_gates` call that can return `not allowed`; assert execution skips when gated.
- `test_greek_cap_tuners_*.py`: one per new tuner. Synthetic outcome data drives expected raise/lower decision.
- `test_max_total_positions_loosen_direction.py`: simulate consistent at-cap performance with positive avg return; tuner raises cap by 1.
- `test_settings_ui_greek_cap_round_trip.py`: POST greek-cap values to settings form; assert profile row updated.
- (Memory rule pin) `test_every_lever_is_tuned.py` already exists. After adding the new tuners, ensure the test passes without adding the three greek caps to the manual-allowlist.

---

## 4. Phasing recommendation

The work breaks naturally into two phases. Phase 1 closes the immediate symptom from the 2026-05-20 incident. Phase 2 completes the paradigm.

### Phase 1 — Address the cash-hoarding symptom (small, focused)
- §3.1 — Drop the at-max pre-filter block.
- §3.3 — Execution orders SELLs before BUYs.
- §3.4 — Wire greek gate into multileg (closes the regression from #189 fix).
- Minimal AI-prompt change in §3.2 (just "you're at N of M positions; SELL something if you want room for a better candidate").
- Tests: pre-filter, execution-ordering, multileg gate.

This is roughly the same blast radius as #189: small, surgical, behaviour-preserving except for the targeted fix.

### Phase 2 — Paradigm completion (broader, less time-critical)
- §3.5 — Greek caps in Settings UI.
- §3.6 — Three new tuners + LOOSEN direction for max_total_positions.
- §3.7 — `param_bounds.py` additions.
- Full AI-prompt context block per §3.2 (cap utilization %, dollar values, breakdown).
- Tests: every tuner + UI round-trip.

Phase 2 doesn't change runtime behavior immediately — it just brings greek caps into the same operator-controlled / self-tuned paradigm as everything else. After Phase 2, every cap can be seen + overridden in the UI AND adjusts autonomously based on outcomes.

---

## 5. What this is NOT

- **Not a new AI action type.** No `SWAP` action. AI just emits SELL + BUY in the same cycle's predictions; execution ordering ensures they cooperate.
- **Not removing caps.** Caps still exist, still enforced as soft bounds. They are tunable, not absent.
- **Not bypassing risk management.** Greek caps still gate option execution; max_total_positions still bounds per-cycle new opens. The "soft" part is that the AI sees the cap and can self-direct around it via SELL+BUY pairs in the same cycle, AND the tuner can raise the cap over time based on outcomes.
- **Not changing the per-instrument execution.** Stock pipeline still trades stocks; option pipeline still trades options (per #189). The cap-tunability is orthogonal to the pipeline separation.
- **Not an atomic SWAP protocol at the broker.** Alpaca doesn't support atomic two-leg "close X, open Y" orders. The within-cycle SELL-before-BUY ordering is enough: SELL frees cash; subsequent BUY uses it. If the SELL fails (broker rejects), the BUY tries against pre-existing cash — if there's enough, fine; if not, the BUY is sized down or rejected. No new failure modes vs today.

---

## 6. Risks I want to surface

- **AI may over-emit SELL when shown "at cap" context.** Mitigation: AI prompt frames "you're at cap" as informational, not directive. The prompt explicitly says "SELL a current holding ONLY if you've identified a better candidate." We monitor the SELL rate of existing positions after Phase 1 lands; if it spikes, refine the prompt language.
- **Cash race condition: BUY processes before SELL fills.** Mitigation: in §3.3, the BUY pass runs AFTER all SELLs are submitted. Alpaca generally fills small market orders in seconds. For limit orders or non-filling SELLs, the BUY may still hit insufficient buying power — broker rejects it; we surface and log; next cycle reconsiders. No state corruption.
- **Greek cap tuners overreact to small sample.** Mitigation: each tuner enforces a minimum sample size (e.g. 30 resolved option trades) before any adjustment. Same pattern as the existing `_safe_change_guarded` helper.
- **Multileg greek gate adds latency.** Mitigation: the greek computation is local arithmetic; no new network calls vs today's single-leg gate. Adds <50ms per multileg open.

---

## 7. Test plan

For each Phase 1 change:
- Unit test the behavior change at the call-site level
- Add regression test using a fixture that reproduces the 2026-05-20 symptom (profile at max_total_positions, new candidate enters cycle, assert AI sees it)
- For execution ordering, instrument the dispatch loop and verify SELL handlers fire before BUY handlers in the same cycle

For each Phase 2 change:
- Tuner tests with synthetic outcome data (mirrors existing tuner test patterns in `tests/test_self_tuning_*.py`)
- UI round-trip test (mirrors existing `tests/test_settings_*.py` patterns)
- `tests/test_every_lever_is_tuned.py` (existing guardrail) starts passing without manual-allowlist entries for the three greek caps

---

## 8. Open questions

1. **Greek caps for crypto?** The schema columns are present on every profile. Crypto profiles' option-trading is limited (Alpaca's crypto API doesn't currently expose options). For now: caps stay on schema but greek-gate effectively no-ops for crypto-only profiles. Revisit if/when crypto options become live.
2. **Should the AI prompt also see the self-tuner's recent adjustment history?** ("Tuner raised your delta cap from 4% to 5% yesterday because realized utilization was 3% over 50 trades.") Could help the AI calibrate confidence in current caps. Adds prompt tokens. Defer to Phase 2 if data supports it.
3. **Where to add the LOOSEN trigger for `max_total_positions`?** Naive heuristic: "at cap for ≥10 cycles AND avg return > 0 AND no top-of-percentile drawdowns." More principled: regress per-position return vs position count and find the marginal-positive cutoff. Simple heuristic first.

---

## 9. Sequencing into the broader work

Lands after #189 (committed). Best executed in two PRs:
- PR 1: Phase 1 (immediate cash-hoarding fix + multileg greek gate)
- PR 2: Phase 2 (paradigm completion)

PR 1 is the user's stated need. PR 2 is the consistency cleanup that completes the operator's "every cap is AI-tunable" vision but isn't time-critical.
