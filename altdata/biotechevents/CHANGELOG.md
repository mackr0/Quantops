# Changelog

Every meaningful code change. Newest at the top.

Pre-commit hook blocks `.py` changes without a same-day entry.
Bypass: `git commit --no-verify` (document why).

---

## 2026-04-25 — Initial release: ClinicalTrials.gov scraper + change detection (Severity: feature)

Local-first scraper for biotech catalysts. Companion to `congresstrades`
and `edgar13f`. Same engineering pattern (raw_filings layer,
parser_version tagging, pre-commit hook, contract tests).

**What ships in v1:**

- ClinicalTrials.gov v2 JSON API scraper. Polls recent updates by
  `lastUpdatePostDate` filter, paginated, polite (1 req/sec).
- `trials` table normalized to: nct_id, sponsor, ticker, phase,
  status, primary_completion_date, conditions, interventions.
- **`trial_changes` table** — every time a trial's `phase`,
  `overall_status`, or `primary_completion_date` differs from what
  we have, a diff row is logged. Querying that table is how you get
  the actionable feed: "these went Phase 2 → Phase 3 this week,"
  "these were just terminated."
- Sponsor → ticker mapping via hand-curated `_SPONSOR_TO_TICKER`
  (~50 major pharma + biotech names; extends as needed).
- CLI: `daily`, `refresh`, `sponsor`, `show`, `changes`, `counts`,
  `runs`.

**Smoke test against real ClinicalTrials.gov:** 200 trials pulled in
one page, 18 mapped to tickers (AstraZeneca, Novartis, Merck, Gilead,
Bayer, etc.). Phase distribution looked right (Phase 2 most common,
then Phase 1, then Phase 3 — matches reality).

**FDA PDUFA scraper: stubbed.** See `scrape_fda.py` docstring for why
(FDA data fragmented, third-party scrapers killed by site changes,
paid alternatives violate the free-infrastructure philosophy).
ClinicalTrials phase transitions are the upstream leading indicator
anyway — when a Phase 3 trial completes, a PDUFA date follows within
6-12 months.

**Engineering invariants enforced by contract tests:**

- User-Agent includes contact email (public-records etiquette)
- Rate-limit detection (HTTP 429/403 → RateLimitedError)
- 1 req/sec politeness delay
- Raw JSON persisted to `raw_filings` BEFORE parsing
- `parser_version` tagged on every parsed row
- Per-page commit (mid-run failures don't lose progress)
- Pagination via `nextPageToken` is always followed
- Idempotent migrations via try/except on ALTER TABLE
- UNIQUE on `(source, external_id)` allows clinicaltrials + fda to
  share IDs without colliding

**Sponsor map seeded with critical names:** Moderna, Pfizer,
AstraZeneca, Vertex, Regeneron, Eli Lilly, Bristol-Myers, Merck,
J&J / Janssen, Genentech / Roche, Novartis, Novo Nordisk, Gilead,
Biogen, AbbVie, Amgen, Sanofi, GSK, BNTX, Alnylam, Ionis, CRISPR,
Editas, Beam, Verve, Recursion, Axsome, Vertex, etc. Maintains
substring match for "Eli Lilly and Company Limited" → LLY.

Tests: 5 files, full normalize/store/parser/contracts/changelog
coverage.
