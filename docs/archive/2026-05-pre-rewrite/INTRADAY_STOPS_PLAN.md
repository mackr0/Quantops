# Intraday Stop Execution Plan (Fix 2)

## The bug

Stops are checked by polling on a 5-minute cycle. When detected, we submit
a market sell at the *current* price — typically far past the stop level
because the price has been moving the whole time we weren't checking.

Two visible patterns from prod:

**Tiny wins** — Trailing stops. IBM rallied to $258.50 then collapsed
to $231.90 in one day. Trail level was $248.54. We didn't catch the
$248.54 cross during the rally; we caught the EOD close at $231.90 and
sold there. Recorded as a $2.70 win on what was an $1,500+ unrealized
gain.

**Loss overshoots** — Stop-losses. AMD threshold was -5%, actual fill
was -7.91% (60% worse). The stock gapped down past the stop level
between cycles; by the time we polled, the price was already 3pp below
where we wanted to exit.

Root cause is the same: detection on a 5-min cycle, fill at the post-cross
market price, no broker-side mechanism to fire AT the stop level.

## The fix

Submit **broker-managed stop orders on Alpaca** so they fire at the stop
price the moment it's touched, not at the next-cycle current price.
Architecture stays compatible with the existing virtual-ledger trade
tracking — the broker fill price flows back through the existing fill-price
reconciliation path.

## Three commits

### Stage 1: Static stop-loss order on entry — addresses loss overshoots

After every BUY (and SHORT) that opens a position, submit:
- Long: `type='stop', stop_price=fill_price * (1 - stop_loss_pct), side='sell'`
- Short: `type='stop', stop_price=fill_price * (1 + short_stop_loss_pct), side='buy'`

Store the broker order_id in a new `protective_stop_order_id` column on
the trades row. Each cycle, sweep any open position lacking an active
stop order and submit one (covers restarts and races).

When AI decides on an early exit (SELL signal), cancel the protective
stop before submitting the exit order.

When the broker stop fires, the existing fill-price reconciliation
picks up the real fill price and updates the trades row.

### Stage 2: Broker take-profit order on entry — locks in wins at threshold

Same pattern but with `type='limit', limit_price=fill_price * (1 + take_profit_pct)`.

When the AI extends winners via the conviction-tp override, submit no
take-profit order for that position (let the trailing stop manage exits).

When the trailing stop fires, the polling system kicks in (Stage 3
removes this).

### Stage 3: Trailing-stop order on entry — addresses tiny wins

Replace the polling-based trailing stop with Alpaca's native
`type='trailing_stop'` order with `trail_percent` derived from the
current ATR/price ratio. The broker tracks the high-water and adjusts
the stop level continuously. When triggered, fills at the trail level.

This eliminates the IBM-style "intraday spike then EOD collapse" problem
because the broker exits the moment the trail level is broken, not at
the next 5-min cycle.

Polling for trailing stops is removed (broker now handles). Polling for
static stop_loss + take_profit also removed (Stages 1+2 handle those).
Polling stays only for AI-driven exits and time-stops on shorts.

## API budget

- 10 profiles × ~50 positions average = 500 active positions
- Stage 1: 1 stop order per new entry. AI typically opens 0-3 trades per
  cycle per profile. ~30 new entries per day across all profiles.
- Each entry costs 1 submit_order call. ~30 calls/day = trivial vs the
  200/min rate limit.
- Stage 3 may require updates if ATR moves significantly between
  cycles — update via cancel+resubmit. Estimate ~10-50 updates per day.

Negligible impact on API budget.

## Failure modes

- **Submit fails** — fall back to existing polling. Log warning.
- **Cancel fails before AI exit** — broker may double-fill (broker stop
  fires after we sell). Detected by reconciliation; the second fill is
  rejected because position is flat.
- **Stop fires but our records lag** — same as today; reconciliation
  picks up the closed position next cycle.
- **Race between broker stop and polling check** — polling on a
  closed position is a no-op (skip if qty=0). Already handled.

## Tests per stage

- Mock Alpaca submit_order. Verify stop_price matches expected.
- Verify cancel-on-AI-exit flow.
- Verify reconciliation handles broker-fired stops.
- Round-trip: simulated price collapse, verify exit at stop level not
  current price.

## Status

- [x] Stage 1: Static stop-loss (commit 3d84543)
- [x] Stage 2: Take-profit (commit b024ab8) — superseded; see "One-order-per-position" below
- [x] Stage 3: Trailing-stop replacement (commit f34b81f)
- [x] Stage 4: Polling defers to broker (commit 7dbbf88) — without this, polling beat broker on every cycle

## Architecture decisions made during deploy

### One-order-per-position

Initial design placed all three (stop + TP + trailing) on every position. Failed in production with `insufficient qty available for order (requested: 19, available: 0)` warnings on every cycle. Root cause: Alpaca treats every open sell-side order as a qty reservation. Submit a stop on 19-share position → all 19 reserved. The TP and trailing then fail.

**Resolution:** ensure_protective_stops places ONE order per position. When `use_trailing_stops=True`, the trailing covers both downside (initial level = entry × (1 - trail)) and profit-lock (level rises with high water). When trailing is disabled, static stop only. Take-profit dropped from broker side; polling TP detection at threshold breach is fine since TP isn't time-critical the way stops are.

Migration sweep cancels stale stop+TP orders from earlier deploys before placing the new single order — without it, the legacy reservations from yesterday's deploy blocked today's trailing placement.

### Polling must defer to broker

Initial Stage 3 deploy showed 0 broker trailing fires across 11 trailing-stop exits in a session — every one fired via the polling fallback. Polling check_trailing_stops on a 5-min cycle detected the breach with the same data the broker had, then `cancel_for_symbol` cancelled the broker trailing before it could fire. Polling beat broker to a worse fill on every cycle.

**Resolution:** `bracket_orders.has_active_broker_trailing(api, db_path, symbol)` checks both the tracked `protective_trailing_order_id` AND broker-side liveness. When both true, polling drops the trigger from its list. Broker fires AT the trail level on the next adverse tick. Polling stays as fallback only when broker isn't actively placed (qty conflict, restart race, etc.).

### Per-position resilience

Original code path had the per-position exit body inline in a `for` loop with no error handling. Alpaca rejections (insufficient qty, etc.) propagated up out of `check_exits` and crashed the whole task — every subsequent position lost protective-stop refresh. Real prod data: 12 TASK FAILs in 2 hours on the Large Cap profile (Account 3, shared across 6 profiles).

**Resolution:** extracted `_process_exit_trigger()`, wrapped each call in try/except. Per-position failures log a WARNING and the loop continues. Three Alpaca rejection patterns reclassified as SKIP, not ERROR: wash-trade (records 30-day cooldown), insufficient qty / buying power, and cross-direction guard (`cannot open a long buy while a short sell order is open` and the symmetric short case).

### Pending orders panel — per-profile filter

10 profiles share 3 Alpaca accounts. The dashboard's pending-orders panel called `api.list_orders` and got every open order on the shared account — so a profile's panel showed orders placed by all sibling profiles. Cross-reference each Alpaca order's `id` against this profile's trades table (union of `order_id`, `protective_stop_order_id`, `protective_tp_order_id`, `protective_trailing_order_id`); only orders this profile placed appear.

## Companion fix: trade quality classification

Pre-Fix-3, `pnl > 0` counted every break-even close as a win. Median win on profile_8 was $43 (~0.09% on $50K notional). After: `|pnl_pct| < 0.5%` → scratch (excluded from win rate), `>= 0.5%` → win, `<= -0.5%` → loss. Scratch rate surfaced separately on the dashboard.

## Companion fix: MFE capture surface

Realized P&L as fraction of available favorable excursion. `mfe_capture.compute_capture_ratio` joins each SELL row's pnl with the matching BUY row's `max_favorable_excursion`. Surfaced to AI prompt when avg < 50% (system leaving money on the table). Negative-capture count flagged separately — trades that lost despite favorable run are the worst pattern.
