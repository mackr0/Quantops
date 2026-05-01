# Options Program — full build plan

The single-leg primitives shipped under "Item 1a" are a toy. A real
options program — the kind that actually moves P&L and survives market
stress — needs the layers below. This document sequences them by
dependency, not by smallness.

**Honesty pass on what existed before this plan:** Item 1a delivered
single-leg covered_call / protective_put / long_call / long_put /
cash_secured_put with sizing constraints and a journal-logged
execution path. None of the structural pieces a real program needs —
aggregate Greeks, multi-leg strategies, hedge rebalancing, roll
mechanics, assignment handling, vol surface analysis — were built.
Calling that "complete" was wrong.

This plan replaces the COMPETITIVE_GAP_PLAN's Item 1a with the
full-fidelity equivalent.

---

## Phase A — Greeks portfolio infrastructure (FOUNDATION)

Every later phase depends on this.

### A1. Greeks aggregator
**What.** A function `compute_book_greeks(positions, prices)` that
returns net portfolio Greeks for an account: total_delta, total_gamma,
total_vega, total_theta, total_rho. Walks open options positions,
recomputes per-contract Greeks via `compute_greeks` at the current
underlying price, multiplies by `qty * 100`, sums. For long
underlying-stock positions, adds `qty * 1` to delta (stock has
constant delta).
**Why.** Without this, we can't tell if the book is net long-vol or
short-vol, can't size new trades against existing exposure, can't
hedge.
**Schema.** New `greeks_snapshots` table or column on
`daily_snapshots`: `total_delta, total_gamma, total_vega, total_theta`
recorded each cycle.
**Acceptance.** Unit-tested on fabricated multi-position books; net
delta computed from a long stock + short call = stock_delta -
call_delta * 100; verified.

### A2. Greeks exposure gates
**What.** Like the existing balance/beta gates: block new options
trades that would push the book past mandate. Three gates:
1. `max_net_delta_pct_of_equity` — directional exposure cap
   (default ~5%; user-tunable).
2. `max_theta_burn_dollars_per_day` — caps how much premium we
   can be paying for time. Long-vol books only.
3. `max_short_vega_dollars` — caps the negative vega exposure
   from short premium. Short-vol books — protects against vol spikes.
**Why.** Same reason every real fund has Greeks limits. Without these,
the AI can keep selling premium until a vol spike wipes the book.
**Acceptance.** Unit tests verify each gate fires when proposed
trade would push past limit; passes when within. Integration test
in `_validate_ai_trades` shows OPTIONS proposals respecting Greeks
gates same way they respect equity sizing gates.

### A3. Greeks dashboard panel on /ai
**What.** Per-profile panel showing current net Greeks, daily theta
burn, vega exposure. Red highlight when within 20% of any gate.
**Acceptance.** Smoke test renders 200; manual visual confirmation.

---

## Phase B — Multi-leg strategy primitives + atomic execution

The bread and butter of pro options programs. Single-leg can't trade
defined-risk credit, can't run iron condors, can't trade vol term
structure.

### B1. Multi-leg spec model
**What.** Extend the existing single-leg builders with a unified
`OptionStrategy` dataclass:
```
@dataclass
class OptionStrategy:
    name: str  # "bull_call_spread", "iron_condor", etc.
    legs: List[OptionLeg]
    max_loss: float
    max_gain: float
    breakevens: List[float]
    net_premium: float  # +debit / -credit
```
And new builders:
- `build_bull_call_spread(symbol, expiry, lower_strike, upper_strike)`
  — long lower call + short upper call (debit, defined risk).
- `build_bear_put_spread`, `build_bull_put_spread`,
  `build_bear_call_spread` — the four 2-leg verticals.
- `build_iron_condor` — short OTM put spread + short OTM call spread
  (credit, defined-risk neutral).
- `build_iron_butterfly` — short ATM straddle + long OTM wings
  (credit, defined-risk pin-risk play).
- `build_straddle` / `build_strangle` — long-vol, short-vol
  variants of each.
- `build_calendar_spread`, `build_diagonal` — term-structure plays.
**Why.** Each strategy expresses a different thesis (directional
defined-risk, range-bound credit, pin-risk, vol expansion, term
structure mispricing). We need every primitive a real options book
trades.
**Acceptance.** Each builder unit-tested with hand-computed max_loss
and breakeven. Iron condor: width × 100 × contracts - net_credit ×
100 × contracts = max_loss; verified.

### B2. Multi-leg atomic execution
**What.** `execute_multileg_strategy(api, strategy, ctx, log)` that
submits all legs as a single Alpaca combo order using
`option_legs=[...]`. Alpaca supports MLEG (multi-leg) order types
for paper accounts. Atomic: all-or-nothing fill; no leg risk.
For brokers / paper accounts that don't support combo orders,
fall back to sequential submission with explicit rollback on
partial-fill detection.
**Why.** Submitting legs one-at-a-time means one leg can fill while
the other is pending and the underlying moves — destroys the spread
P&L profile. Atomic is required.
**Acceptance.** Mocked-API tests verify combo order submitted with
correct legs; failure on leg 2 of a sequential fallback triggers
rollback; live paper test on Alpaca confirms a real combo order
goes through.

### B3. Multi-leg strategy advisor
**What.** Extend `options_strategy_advisor` to recommend multi-leg
setups based on the position + vol regime + AI's directional view:
- IV rich + bullish on name → bull put spread (sell premium, defined risk)
- IV rich + range-bound → iron condor on the range
- IV cheap + directional conviction → debit spread (debit pays for
  cheap vol, defined risk)
- IV cheap + uncertain timing → calendar spread (long back-month
  short front-month, profits as front decays)
- Earnings approaching + IV expansion → iron condor pre-earnings
  (collect IV crush after)
**Why.** The single-leg advisor only sees covered_call / protective_put.
Multi-leg expands the strategy menu by ~10x.
**Acceptance.** Each rule unit-tested against representative
positions. Render output includes the new strategy types.

### B4. AI prompt vocabulary expansion
**What.** Add multi-leg PAIR_TRADE-equivalent action vocabulary:
`MULTILEG_OPEN`, `MULTILEG_CLOSE`. The AI proposes with
`{strategy_name, symbol, legs}`; the validator looks up the strategy
spec from the advisor; the executor calls `execute_multileg_strategy`.
**Acceptance.** End-to-end: AI prompt includes multi-leg recs,
proposes MULTILEG_OPEN, validator passes through, executor submits.

---

## Phase C — Lifecycle (open → manage → close)

### C1. Roll mechanics
**What.** New `_task_options_roll_manager` that runs daily during the
final week before any held option's expiry. For each near-expiry
contract:
- If the current position is profitable AND the AI's thesis on the
  underlying still holds → roll forward (close current, open same
  structure at next expiry). The AI gets a ROLL action proposal.
- If the position is at max profit on a credit strategy (≥80% of
  max gain captured) → close early, don't wait for expiry.
- If the position is showing a loss but pre-defined exit criteria
  haven't fired → leave it.
**Why.** Without roll mechanics, profitable positions get force-closed
at expiry losing the future premium. Pro programs roll continuously.
**Acceptance.** Tests on each branch; integration with the lifecycle
sweep.

### C2. Assignment detection + reconciliation
**What.** Extend `options_lifecycle.sweep_expired_options`:
- For SHORT options (covered_call, CSP, credit spreads): if the
  short leg expires ITM, assignment occurred. The lifecycle sweep
  must:
  1. Detect the underlying position change (CSP → +100 shares;
     CC → -100 shares).
  2. Update the journal: original short option → status="assigned"
     with the realized premium as P&L, new stock leg logged as
     synthetic BUY/SELL.
  3. Update the AI's track-record so it knows assignment happened.
- For LONG options expiring ITM: auto-exercise (Alpaca handles).
  Detect post-expiry, log the synthetic stock leg.
**Why.** Without this, an assigned CSP shows as "expired" with
wrong P&L and the journal silently has 100 phantom shares the
ledger doesn't know about.
**Acceptance.** Tests for each path: CSP-assigned, CC-called-away,
long-call-exercised, long-put-exercised.

### C3. Wheel strategy automation
**What.** New `wheel_state_machine.py`. State per (profile, symbol):
`cash` → `csp_open` → (assignment? → `shares_held` → `cc_open` →
(called_away? → `cash`) | (cc_expires_otm → `shares_held` → `cc_open`)
| `csp_expires_otm → `cash` → `csp_open`).
The state machine drives strategy proposals: from `cash` propose CSP;
from `shares_held` propose CC. Logged transitions visible on dashboard.
**Why.** The wheel is one of the highest-Sharpe options income
strategies. Automating it generates consistent premium income on
stable names without AI cycle-by-cycle decisions.
**Acceptance.** State-machine unit tests; dashboard panel shows
wheel state per active symbol; integration: a profile with `wheel_mode`
on auto-cycles through the states.

---

## Phase D — Hedging

### D1. Dynamic delta hedging
**What.** For positions where delta exposure matters (long calls, long
puts, ratio spreads), continuously rebalance the underlying-stock
hedge to keep net position delta near a target. Runs as part of the
existing `Check Exits` cycle. Threshold-based: only rebalance when
|delta_drift| > rebalance_threshold to avoid churning on noise.
**Why.** A long-call position is a long-delta position. As the stock
moves, delta changes (gamma). Without rebalancing, a long-call
"insurance" position becomes a directional bet.
**Acceptance.** Tests on price-path simulation: held a long call,
stock rose 5%, hedge sold N shares to bring delta back to target.

---

## Phase E — Volatility analysis

### E1. IV term structure
**What.** New `iv_term_structure(symbol)` returns IV by expiry across
the chain. Normal contango (back-month > front-month IV) vs
backwardation (front > back). Backwardation usually means front-month
event risk priced in.
**Acceptance.** Unit-tested on representative chain data.

### E2. Vol skew
**What.** OTM put IV vs OTM call IV. Skew widening = market pricing
in tail risk. Skew compressing = complacency.
**Acceptance.** Same.

### E3. Realized vs implied vol
**What.** Compute trailing realized vol (60d annualized) per symbol;
compare to current ATM IV. Spread = vol risk premium. Sell premium
when IV >> RV; buy when IV << RV.
**Acceptance.** Same.

### E4. Vol regime gate
**What.** Surface E1/E2/E3 to the AI prompt + advisor so multi-leg
strategy selection respects vol regime (sell condors when IV rich
vs realized, buy calendars when term-structure flat, etc.).

---

## Phase F — Earnings/event opportunism

### F1. Earnings vol plays (replace avoid-earnings)
**What.** Replace the blanket `avoid_earnings_days` with conditional
logic:
- Pre-earnings + IV high vs trailing realized → sell premium
  (iron condor capturing IV crush).
- Pre-earnings + IV unexpectedly cheap → buy straddle (rare; market
  is mispricing event risk).
- Post-earnings → time-stop early (premium decays even faster
  post-event).
**Why.** Earnings are the most reliable IV crush event in the market.
Avoiding them entirely leaves predictable premium on the table.
**Acceptance.** A/B trace on past earnings shows the new logic
captures premium the old logic skipped.

### F2. Macro event plays
**What.** FOMC, CPI, NFP days. IV expands into these, crushes after.
Same crush-capture logic as F1.

---

## Phase G — Data quality (deferred until real money)

### G1. Real-time options chain feed
**What.** Replace yfinance options chains with a real-time feed. Two
options:
- Polygon Options ($199/mo for unlimited) — cleanest, NBBO updates
  sub-second.
- Alpaca Options Data subscription (cheaper, lower-latency than
  yfinance but still feed-delayed).
**When.** Defer until real-money phase. Paper trading on yfinance
data is honest about its limitations.

---

## Phase H — Backtest infrastructure

### H1. Options backtester
**What.** Historical options chain replay + multi-leg P&L simulation.
Required to validate any new strategy before going live.
**Effort.** Major build (~2 weeks). Requires historical IV surfaces,
either bought (Polygon historical $99/mo, OptionMetrics expensive)
or scraped from CBOE settlement files.
**When.** Last; everything above is more important.

---

## Sequencing (top-down implementation order)

| Order | Phase | Item | Effort | Edge |
|---|---|---|---|---|
| 1 | A | A1 Greeks aggregator | 1d | foundation |
| 2 | A | A2 Greeks exposure gates | 1d | risk |
| 3 | A | A3 Greeks dashboard panel | 0.5d | observability |
| 4 | B | B1 Multi-leg spec + 4 vertical builders | 2d | LARGE |
| 5 | B | B2 Multi-leg atomic execution | 2d | LARGE |
| 6 | B | B1+ Iron condor / straddle / strangle / calendar | 1d | LARGE |
| 7 | B | B3 Multi-leg advisor | 1d | LARGE |
| 8 | B | B4 AI prompt vocabulary | 0.5d | LARGE |
| 9 | C | C2 Assignment detection | 1d | correctness |
| 10 | C | C1 Roll mechanics | 1d | medium |
| 11 | C | C3 Wheel automation | 2d | medium |
| 12 | D | D1 Dynamic delta hedging | 1.5d | medium |
| 13 | E | E1-E4 Vol surface analysis | 2d | medium |
| 14 | F | F1-F2 Earnings/event opportunism | 1d | medium |
| 15 | H | H1 Options backtester | 10d | foundation |

**Total: ~30 working days for everything except H.**

## Acceptance criteria for "options program is complete"

1. Greeks aggregated, gated, dashboarded ✓
2. All 8 multi-leg primitives ship with builders + tests ✓
3. Multi-leg atomic execution ships with prod live trade ✓
4. Multi-leg advisor recommends regime-appropriate strategies ✓
5. AI can propose any of the strategies and they execute ✓
6. Assignment detection reconciles correctly ✓
7. Rolls fire on near-expiry profitable positions ✓
8. Wheel runs end-to-end on a stable name ✓
9. Delta hedging keeps long-vol positions near target delta ✓
10. Vol regime drives advisor recommendations ✓
11. Earnings days are TRADED, not avoided ✓

Until every box is checked, this isn't complete and won't be called
that.
