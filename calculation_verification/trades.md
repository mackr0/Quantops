# /trades — Calculation Register

Computation sources: `views.py` route `trades` (~2376) with
`_trades_pnl_summary` (539–656), `_get_trade_history_for_profile`
(680–730), `_enrich_trade_history_with_live_pnl` (739–788);
`journal.py` virtual-book lenses; `spread.py` for multileg headers;
templates `trades.html` + `_trades_table.html`.

Verification pass: 2026-08-13 at commit `70ffbe2`. The underlying
cash/position algebra (`get_virtual_cash`, `get_virtual_positions`)
was independently line-verified during the 08-03→08-13 incident work.

---

## Headline P&L block

**Realized P&L (+ closed-trade count)** — `Σ pnl` and `COUNT(*)` over
rows with `pnl IS NOT NULL`, across the selected scope (one profile
or all non-baseline profiles). *Why:* realized is journal truth and
must not move with market marks. Marked "· incomplete" when any
profile's journal was unreadable — a knowingly short sum is labeled,
never silently presented as whole. **SUSPECT — S6:** this sum does
NOT exclude `data_quality`-tagged rows, while the performance page's
Total P&L (via `_gather_trades`) DOES. The two pages can legitimately
disagree by exactly the tagged rows' pnl. Needs one policy.

**Total P&L** — `Σ live equity − Σ initial_capital` over scope.
Equity per profile from the broker (real accounts) or
`get_virtual_account_info` (cash algebra + positions marks;
fill-true, option-multiplied, dead-status-excluded). Initial capital
from the profile record. *Why:* "how much money exists vs what was
started with" — the only definition that ties to the dashboard.
**VERIFIED**

**Unrealized P&L (open)** — computed as a RESIDUAL:
`total − realized`, not as a sum of open-position marks. *Why:*
guarantees the three headline figures always tie exactly; a
marks-based sum drifts from the equity identity by pricing noise.
Trade-off: any error in either input lands silently in this cell —
acceptable because both inputs carry their own integrity gates, but
auditors should know it is derived, not measured. **VERIFIED**
(with the derivation explicitly documented)

**"N order(s) never filled — not P&L"** — count of rows in
`('canceled','expired','rejected','done_for_day')`. *Why:* makes the
dead-status exclusion visible instead of leaving an unexplained gap
between row count and P&L count. **VERIFIED**

**Unavailability reasons** — three mutually exclusive branches
(journal unreadable > live valuation unfetchable > no capital
baseline), each rendering "unavailable" with scope ("K of N
profiles"). Degraded books are never cached and never rendered as
numbers (README convention 4). **VERIFIED**

## Result-set counts

**"{N} total" + pagination** — `N = len(fetched rows)` AFTER filters
and AFTER the per-profile row limit (200 single / 100-per-profile
all). **SUSPECT — S7:** a page with more history than the limit
displays the cap as if it were the true filtered total ("200 total"
when 1,400 rows match). Cosmetic but audit-relevant. *Proposed:*
either a real `COUNT(*)` per filter or a "showing latest 200" label.

**Page links / slice math** — ceil-division pages of 50, window-2
page links with real-page gap filling. **VERIFIED (code)**

## Per-trade rows

**Time** — UTC journal timestamp rendered in ET. **VERIFIED (code)**

**Qty / row notional** — `qty × price × (100 if option)`; qty is
broker-trued on terminal fills. **VERIFIED**

**Price** — decision price, backfilled to broker fill when
originally missing; dead orders show their status word instead of a
price. **VERIFIED**

**Live mark "@ $X (±Y%)"** — position `current_price` vs row price,
sign-flipped for shorts; stamped only on the most-recent row per
position key so an older lot never wears a fresher lot's mark.
**VERIFIED**

**Realized row P&L % ** — `pnl / (exit_proceeds − pnl) × 100`, i.e.
percent of IMPLIED cost basis. *Why:* the entry row's basis isn't on
the exit row; proceeds-minus-pnl reconstructs it exactly under
fill-true accounting. Known hazard: when a corrupted row's pnl ≈
proceeds the implied basis →0 and the percent explodes — that exact
class is caught upstream by the `data_quality` tagger (implied
basis < $1 ⇒ tagged, and tagged rows wear the EXCLUDED badge).
**VERIFIED**, hazard documented and mitigated.

**Unrealized row P&L** — position `unrealized_pl`/`plpc`: long
`(cur − entry)/entry`, short `(entry − cur)/entry`, option ×100.
Enrichment is skipped entirely on the Realized tab so a live mark
can never leak onto a locked-in row. **VERIFIED**

**Multileg leg P&L ("this leg")** — the leg's own unrealized mark,
dollars only, no percent. *Why no %:* a leg's own premium is a
misleading base (see performance register, episode discussion).
**VERIFIED**

**AI Confidence** — inherited from the ENTRY row onto auto-exit rows
at write time. *Why:* the exit executes the entry's thesis; a blank
would read as "no AI involved." **VERIFIED (code)**

**EXCLUDED badge** — rows carrying `data_quality`; rendered but
excluded from analytics (see S6 for the one aggregate that still
includes them). **VERIFIED**

## Multileg spread header

**"N legs" / net credit-or-debit** — legs grouped by shared
`order_id`, net premium `Σ ±price×qty×100` (buys add, sells
subtract), label by sign. **VERIFIED**

**Header unrealized ($, %)** — `Σ leg marks`, CLAMPED on the loss
side at the spread's structural max loss (debit spreads: net debit;
credit spreads: `(strike_width − credit) × 100 × qty`); percent is
over that same cap. *Why the clamp:* option mark noise (a $0-bid
short leg) can show a defined-risk spread losing more than its
mathematical maximum; the clamp displays the bounded truth.
**VERIFIED**

## Detail row

**Market value** — `±current × qty × mult` by side. **VERIFIED**
**Stop / Target** — journal columns, shown as stored; the
struck-through "let winners run" variant is dashboard-only by
construction. **VERIFIED (code)**
**Slippage %** — `(fill − decision)/decision × 100`, stored at
fill-truing time; rendered only when both prices exist. **VERIFIED**

## Async widgets (base template, appears on every page)

**Issues badge** — errors+criticals (else warnings) occurrence sum
over 24h from the issues collector. **VERIFIED (code)**
**Symbol modal** — Alpaca snapshot price, day-change
`(price/prev_close − 1)×100`, 252-bar 52-week range, 30-bar average
volume, compact-formatted market cap from the company profile.
**VERIFIED (code)**

## Deliberate absences (documented so auditors don't hunt)

No win-rate, no slippage aggregate, no risk metrics on this page —
those live on /performance and /ai-performance with their own
registers. The only filter-reactive count is the result-set total
(S7); headline sums are profile-scoped only, by design, with the tab
merely choosing which figure renders large.

---

## Open items for this page

| ID | Item | Concern |
|---|---|---|
| S6 | Realized P&L includes data_quality rows | disagrees with /performance total_pnl policy |
| S7 | "{N} total" is post-limit | cap displayed as a true total |
