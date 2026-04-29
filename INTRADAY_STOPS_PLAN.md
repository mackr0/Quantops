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
- [x] Stage 2: Take-profit (commit pending)
- [ ] Stage 3: Trailing-stop replacement
