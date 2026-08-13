# /admin — Calculation Register

Sources: `views.py admin` (~5840), `ai_cost_ledger.spend_summary`,
template `admin.html`. Verification pass: 2026-08-13 at `70ffbe2`.

**Users (N) / row fields** — all `users` rows (active or not), dates
ET-rendered. **VERIFIED (code)**

**API Calls Today (per user)** — Σ over the user's profiles of the
ledger's today-count (ET-midnight anchored), per-profile failures
logged and contributing 0. **SUSPECT — S19 (two parts):** (a) the
per-user dollar cost is computed on every render and never displayed
— dead work and a missing column; (b) the profile DB path here is the
BARE RELATIVE filename — the exact CWD-fragility class fixed
elsewhere on 2026-07-24 (works only because gunicorn's CWD is the
repo; a CWD change silently zeroes the column instead of erroring).

## Open items for this page

| ID | Item | Concern |
|---|---|---|
| S19 | API-calls column plumbing | dead cost computation; CWD-fragile relative DB path |
