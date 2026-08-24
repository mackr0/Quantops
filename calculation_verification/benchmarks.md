# Virtual benchmarks — Calculation Register

Source: `virtual_benchmarks.py`; consumed by
`comparative_returns.build_payload` (dashboard comparative-returns
chart) and, later, the Learning Scoreboard. Created by the Experiment 2
reset script (`full_fresh_start_2026_08_24.py` step 4b), marked to
market by the scheduler's daily-snapshot task. Decision D6, docs/25.
Verification pass: 2026-08-23 (fixture-verified; live verification
after the first daily mark).

**Selection (once, at creation)** — Buy-Hold-SPY: `qty = floor(capital
× (1 − 0.05) / close_SPY)`; Random: the universe shuffled by a seeded
RNG, first 5 symbols that have a price, each
`qty = floor((capital × 0.95 / 5) / close)`; `cash = capital − Σ qty ×
entry_price`. Seed = sha256 of the benchmark name, masked to 63 bits.
*Why:* mirrors the retired broker path's 5% cash buffer and 5-pick
equal weight so Experiment 2's nulls are comparable to Experiment 1's;
the sha256 seed replaces Python's per-process-salted `hash()` (the
old seed was only stable because a fire-once guard never let it
re-run). **VERIFIED** (fixture: SPY 500 → 475 shares, $12,500 cash,
day-zero equity exactly the capital).

**Activation at the first session's open (2026-08-23, operator
ruling)** — the reset creates each benchmark PENDING: symbols fixed
(eligibility by the latest close and the $10 floor), `qty = 0`, all
capital as cash, no snapshot. On the first daily mark on/after
`start_date` (the next market open per `market_calendar`), shares are
set from that day's OPEN: buy-hold `qty = floor(capital × 0.95 /
open_SPY)`; random `qty = floor((capital × 0.95 / 5) / open)` per
name; `cash = capital − Σ qty × open`; `activation_date` recorded; the
day is then marked at the close like any other. *Why:* the arms can
first trade at that open, so that is the comparable start — a
Friday-close entry would give the benchmark a different starting
print than the arms. If any holding's open is unavailable, activation
is deferred with an ERROR log (nothing fabricated). Activation runs
per scheduler cycle during the session (`activate_pending` — shares
set minutes after the open, no snapshot written); the daily equity
row comes only from the end-of-day snapshot task (2026-08-24 fix:
hooking both into the evening task left benchmarks pending all of
day one).
**VERIFIED** (fixture: pending → nothing written before start;
Monday open 502 / close 510 → qty 473, cash 12,554, equity cash +
473 × 510; missing open → deferred, nothing written).

**Daily equity** — `equity = cash + Σ qty × latest_close`, one row per
ET date (`INSERT OR REPLACE` on benchmark/date). A holding without a
price means NO row for that benchmark that day and an ERROR log — a
fabricated equity is worse than a gap (README convention 4).
**VERIFIED** (fixture: 475 × 510 + 12,500).

**Dividends** — cash dividends from Alpaca's corporate-actions
announcements (rolling 89-day window by ex-date; only
`ca_type=dividend`, `ca_sub_type=cash`, rate > 0) are credited to cash
ONCE, on the first mark on/after the payable date, when the ex-date is
on/after the benchmark's start; uniqueness on (benchmark, symbol,
ex_date). *Why:* Alpaca paper credits dividends to broker-held arms;
without this the benchmark would trail by SPY's ~1.2%/yr yield.
**VERIFIED** (fixture: before payable → 0; on payable → credited once
across two runs; ex-date before start → ignored; parser drops stock
dividends and splits).

**Return series** — `return_pct = (equity / day-zero equity − 1) × 100`,
same shape as a profile's series; `profile_id` is the negative
benchmark id; `strategy_type` is the kind so the chart styles it like
the old baseline profiles. **VERIFIED** (fixture).

**Not modeled (stated):** one-time entry slippage (a few bps, once);
splits/spin-offs (large-cap universe; a >40% move from entry within
10 days, or >40% equity move in one mark, logs a WARNING naming the
holding so it cannot pass silently).

**Daily cadence guard** — `run_daily_if_due` marks only when some
enabled benchmark lacks today's row; it is hooked into the per-profile
daily-snapshot task, so the first profile's snapshot triggers it and
the rest find nothing due. **VERIFIED** (fixture: second call returns
None, prices fetched once).

**Dashboard "Reference Benchmarks" table** — one row per benchmark:
status (pending with its activation date, or active since its
activation open), holdings (symbols; with share counts once active),
start capital, Value, `return = value / capital − 1`, dividends
credited to date, last-marked date. **Value (2026-08-24)** is a LIVE
mark during the session — `cash + Σ qty × latest bar close`, one bulk
quote call, labeled "live" — so benchmarks move on the dashboard the
way profile equity does; when live prices are unavailable it falls
back to the latest end-of-day snapshot (labeled "close"); with
neither, absent — never fabricated. The live mark is display-only:
the persisted daily series is written solely by the evening snapshot
task. **VERIFIED** (fixtures: live mark math + nothing persisted +
fallback labeling; Flask-client smoke test).

## Open items for this page

None at creation. Add the first live mark's numbers here after the
2026-08-24 open.
