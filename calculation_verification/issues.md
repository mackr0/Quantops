# /issues — Calculation Register

Sources: `issues_collector.py` (`collect_issues`, `issues_count`),
route `views.py issues_page` (~1040), template `issues.html`.
Verification pass: 2026-08-13 at `70ffbe2`.

**Window ("last N hours")** — query param clamped to [1, 168].
**VERIFIED**

**ERROR / WARNING event cards** — template sums of `occurrences`
over groups by level (ERROR+CRITICAL together). Groups are
`(source, level, signature)` where the signature strips numbers/ids/
timestamps so near-identical spam collapses into one row. *Why:* a
retry storm should read as one issue ×N, not N issues. **VERIFIED**

**Distinct groups / total events** — `len(groups)` and
`Σ occurrences` — computed after the level filter, so "total events"
equals the two cards' sum only on the unfiltered view (documented so
an auditor doesn't flag the mismatch as a bug). **VERIFIED**

**Count / first-seen / last-seen per row** — group counters with
min/max timestamps, ET-rendered; audit-driven rows show "live
snapshot" (they are recomputed each view, not historical events).
Sort: severity rank, then most-recent first. **VERIFIED (code)**

**Message-embedded figures** (qty drift, cash/value/basis parity,
equity identity, reconciler heartbeat age) — produced by the
respective audit modules; their formulas are registered where they
live (see [trades.md](trades.md)/[README](README.md) conventions and
the aggregate-audit code). The issues page renders them verbatim.
**VERIFIED (pass-through)**

**Equity identity (2026-08-24 revision)** — `drift = (cash + Σ mv) −
(init + leg-derived realized + unrealized)`, where realized comes from
`journal.compute_leg_realized` (fill-true FIFO, the same
`COALESCE(fill_price, price)` basis as cash, same instant) — NOT the
pnl column. This makes the identity hold through fill-confirmation
windows (the p230 first-session false ERROR: a submit-time estimate,
then the decision-vs-fill spread of a pending exit). A wrong pnl
COLUMN surfaces as its own `pnl_column.profile_N` ERROR row (stored vs
leg-derived on closed rows), never as equity drift. **VERIFIED**
(regression fixtures: the p230 window at drift 0; injected column
errors caught to the cent by the new finding).

**Nav badge** — same collector at a FIXED 24h window regardless of
the page's window; badge shows errors else warnings. Documented as an
intentional page-vs-badge window difference. **VERIFIED**

## Open items — none. (The page-vs-badge window difference and the
filtered-total nuance are documented behaviors, not defects.)
