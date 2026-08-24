# /dashboard — Calculation Register

Computation sources: `views.py` route `dashboard` (~1171) +
`_load_profile`, `_enriched_positions`, `_dashboard_totals_payload`,
and the live-refresh APIs (`/api/dashboard-totals`,
`/api/portfolio/<id>`, `/api/positions-html/<id>`,
`/api/cycle-data/<id>`, `/api/scan-status/<id>`, `/api/activity`,
`/api/sector-rotation`, `/api/comparative-returns`);
`cost_guard.py`, `ai_cost_ledger.py`, `mfe_capture.py`,
`comparative_returns.py`, `journal.py` lenses, `spread.py`.

Verification pass: 2026-08-13 at `70ffbe2`. Position/cash lenses were
line-verified during the August incident work.

---

## Banners

**AI-spend banner ("$X of $Y ceiling")** — today = Σ per-profile
`ai_cost_ledger.estimated_cost_usd` since ET midnight; ceiling =
operator override if set, else `max(floor, 7-day trailing avg ×
multiplier)`, labeled "user-set"/"auto-set". *Why trailing-auto:* a
fixed ceiling either strangles growth or never fires; the multiplier
of recent normal keeps it an anomaly alarm. **VERIFIED**

**"TRADING HALTED — N profiles"** — count of profiles with
`trading_halted` set, each row naming reason and time. **VERIFIED**

**Scan failures (last hour)** — at most one row per profile: the
latest failed `task_runs` row in the trailing hour. *Why capped:*
a storm shows breadth (which profiles) not depth (row spam).
**VERIFIED (code)**

## Portfolio summary table (per profile)

**Equity / Cash / Buying power** — broker fields for real accounts;
virtual books via `get_virtual_account_info`: cash = initial − Σbuys
+ Σsells (fill-true, option-×100, dead-statuses excluded), equity =
cash + Σ position marks, buying power = max(cash,0). Degraded books
render "Not connected" — never numbers (README conv. 4). **VERIFIED**

**P&L / P&L %** — `equity − initial_capital` and
`(pnl / initial_capital) × 100`; initial capital from the profile
record. *Why vs the performance page's Total Return:* this is LIVE
equity vs baseline; /performance compounds daily snapshots — the two
legitimately differ intraday, and the card's tooltip says so
(2026-07-15). **VERIFIED**

**Medals 🥇🥈🥉** — top 3 by P&L %, recomputed live client-side with
the identical formula the server uses for other pages. **VERIFIED**

**Positions count** — length of the enriched position list (one row
per broker position; spread grouping stamps, never collapses), API
refresh counts the raw position list — same cardinality by
construction. **VERIFIED (code)**

**AI cost today (per profile / total)** — per-profile column = the
profile's TRADING ledger sum (deliberately un-rounded so totals never
drift from parts by rounding). The **headline "AI Cost Total" is
ALL-IN** (2026-08-24, operator catch): trading + shadow-eval spend,
with the split shown beneath it — before the fix the headline showed
trading only while the shadow lines under it summed to more than the
"total". Client- and server-side use the same sum; "by model" and
"shadow eval" breakdowns are `GROUP BY provider, model` sums over the
two ledgers (kept separate so shadow spend never contaminates
per-profile trading cost; each side has its own daily cap —
`daily_cost_ceiling_usd` and `shadow_daily_cost_cap_usd`).
Expected shapes, so they aren't re-reported as anomalies: shadow
calls ≈ 2× primary calls (only specialist purposes are shadowed,
each fanning to the 3 rival arms), and primary $/call runs higher
than shadow $/call (the unshadowed batch-selection prompts are the
largest). **VERIFIED** (2026-08-24 live figures: trading $4.29 +
shadow $5.66; by-model row summed exactly to the trading total).

## Schedule bar / scan status

**"Next: Nm" / "Due"** — `interval − (now − last scan run)`, floor 0,
from `task_runs` + the user's scan interval; live poll variant uses a
status file treated stale after 300s. **VERIFIED (code)**

## AI Brain panel (per profile)

**Stop-to-TP ratio ("R (N stops / M tps, 30d)")** — closed exits
grouped by strategy over 30 days (data-quality-excluded): stops =
stop_loss+trailing+short variants, tps = take-profit variants;
refuses below 10 total (README conv. 5). *Why:* a ratio drifting
high means stops do the exiting — the tuner's early-warning number.
**VERIFIED**

**MFE capture ("N% (M trades)")** — for the last ≤50 profitable-MFE
exits: realized % ÷ max-favorable-excursion %, averaged. Entry is
matched as the most recent prior buy row for the symbol carrying an
MFE — a heuristic join, adequate for a coaching stat, and documented
as such here. *Why:* measures how much of the best-seen gain exits
actually keep. **VERIFIED (code, heuristic join noted)**

**Trades-selected lines** — size ("X% equity"), confidence ("N%"),
badges (REJECTED/EXECUTED-AS/BLOCKED/GATED) joined from
`trades`/`broker_rejections`/`trade_drops` by cycle. **SUSPECT —
S8:** the panel's multileg/options branches render `t.contracts`,
`t.strategy_name`, `t.option_strategy`, but the cycle writer persists
only `{symbol, action, size_pct, confidence, reasoning}` — those
fields are never present, so option trade lines render with missing
("undefined") sizing. Display defect, not a books defect.

**Candidates table (Score /4, RSI, ADX, MFI, Vol ×, short %, Reddit,
news count)** — verbatim pass-through of the cycle's shortlist
snapshot; no page-side math beyond `toFixed`. **VERIFIED (code)**

## Account stats cards

Same equity/cash/BP/P&L chain as the summary table, 2-dp variants,
15s refresh recomputing P&L client-side from the same inputs.
**VERIFIED**

## Open positions table

Shared row macro with /trades (see [trades.md](trades.md) items:
qty/notional, entry, live mark with short sign-flip, AI confidence
lookup from the most recent matching entry row, four-branch P&L cell,
spread header with loss-side clamp at structural max loss, detail-row
market value / stop / target / slippage). Dashboard-specific: rows
are built from LIVE broker positions with journal metadata joined by
OCC-or-symbol, and the "let winners run" struck-through target
renders only here (conviction ≥ the profile's threshold, default
70%). **VERIFIED**

## Pending orders

Own-order-filtered broker open orders (journal order-id allowlist —
never a sibling profile's); price cell precedence limit → stop
(+trail % or $) → trail-only → "market". **VERIFIED**

## Reference benchmarks table (2026-08-23)

**Every cell** — see [benchmarks.md](benchmarks.md): status
(pending/active with date), holdings, start capital, latest-snapshot
value, `return = value / capital − 1`, dividends credited, last mark.
Virtual — no broker account; Value/Return absent until the first
mark. **VERIFIED** (fixture + Flask-client smoke test).

## Comparative returns chart

Per profile: `((equity / first_snapshot_equity) − 1) × 100` per day;
baseline profiles (BuyHoldSPY, Randoms) styled distinctly. *Why
normalized to first snapshot:* comparability across different
capital bases. **VERIFIED**

## Sector rotation

Per sector ETF over 30 bars: 5-day and 20-day % change, chip
thresholds ±1%. **VERIFIED (code)**

## Activity ticker

"N of M" = feed offset over `COUNT(*)` of the user's activity log;
free-text entries carry no page-side math. **VERIFIED (code)**

## Inert code (documented so auditors don't chase it)

A countdown-timer script for removed elements returns immediately;
the global medals context-processor is unused by this page (it
computes its own). **VERIFIED (code)**

---

## Open items for this page

| ID | Item | Concern |
|---|---|---|
| S8 | AI Brain multileg trade lines | renders contract/strategy fields the cycle snapshot never persists |
