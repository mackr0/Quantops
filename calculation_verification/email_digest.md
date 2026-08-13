# Daily email digest — Calculation Register

Source: `notifications.notify_daily_summary` (~502–660), fed by
`client.get_account_info` / `get_positions` (virtual-book lenses),
`journal.get_trade_history` / `get_performance_summary`,
`ai_tracker.get_ai_performance`, `portfolio_manager.get_risk_summary`.
The separate shadow-eval daily email is registered in
[shadow.md](shadow.md)'s machinery (same metric engine as the page).
Verification pass: 2026-08-13 at `70ffbe2`.

**Account overview (equity/cash/BP/unrealized)** — the standard
virtual-book lenses (fill-true, ×100 options, dead-status excluded);
degraded books cause the SECTION TO BE OMITTED rather than mailing
the initial-capital fallback (README conv. 4, enforced here by an
explicit raise). Buying power clamps at 0 — negative cash is visible
in Cash, not BP. **VERIFIED**

**Open positions table** — the gvp FIFO lens rows verbatim; a failed
quote falls back to entry price (renders zero P&L rather than an
error — documented; the page equivalents behave the same).
**VERIFIED**

**Trades today** — last-200 journal rows filtered to today.
**SUSPECT — S17:** (a) "today" is an ET date prefix-matched against
UTC-stored timestamps — trades between ET midnight and UTC midnight
bucket to the wrong day; (b) the 200-row cap applies BEFORE the date
filter, so a busy day silently truncates. Also the Price column
shows the decision price, not the fill (inconsistent with fill-true
everywhere else).

**AI performance block** — counts, blended win rate (HOLDs
included), profit factor over summed PERCENTAGES (can render `inf`),
avg move on BUYs/SELLs without sample sizes. **SUSPECT — S18:** the
email's win rate silently differs from /ai's directional definition;
the percent-summed profit factor is not the dollar profit factor an
auditor expects; and averages without n invite over-reading (a
1-sample average renders like a 500-sample one).

**Risk summary** — positions/slots, cash %, invested % (Σ|market
value| — shorts ADD to invested, by design), total unrealized
(computed twice in one email from the same inputs — both shown;
noted), largest position weight. **VERIFIED (code)**

**All-time trade performance** — the journal summary
(data-quality-excluded; pnl≤0 counts as loss — part of the S12
definitional cleanup). **VERIFIED (code)**

## Open items for this page

| ID | Item | Concern |
|---|---|---|
| S17 | Trades-today selection | ET-vs-UTC day boundary; cap-before-filter; decision price shown |
| S18 | AI block definitions | blended win rate; percent-summed profit factor (`inf`); no sample sizes |
