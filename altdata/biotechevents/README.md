# biotechevents

Local, on-demand scraper for clinical-trial milestones (ClinicalTrials.gov)
and FDA decision events. Built to surface the leading indicators for
biotech catalysts — phase transitions, primary completion dates, status
changes, AdCom meetings, PDUFA decisions.

Companion to `congresstrades` and `edgar13f`. See `ALTDATA_PLAN.md` in
the QuantOpsAI repo for context.

## Status

- ✅ **ClinicalTrials.gov v2 API** — fully working. Pull recent updates
  or backfill specific sponsors.
- 🟡 **FDA PDUFA calendar** — stubbed. The FDA's data is fragmented
  across unstructured pages with no clean API; documented why in
  `scrape_fda.py`. Phase transitions in clinical-trial data are the
  most actionable upstream signal anyway (PDUFAs follow ~6-12 months
  after Phase 3 completion).

## Quickstart

```bash
cd /Users/mackr0/biotechevents
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
./scripts/install-hooks.sh

# Smoke test: 1 page (~200 trials) of recent updates
python -m biotechevents.cli refresh --days 7 --max-pages 1

# Full daily refresh
python -m biotechevents.cli daily

# Backfill a specific company's trials
python -m biotechevents.cli sponsor "Moderna"

# Query
python -m biotechevents.cli show --ticker MRNA
python -m biotechevents.cli show --phase PHASE3 --status RECRUITING
python -m biotechevents.cli show --completion-after 2026-01-01

# What changed recently (the actionable signal)
python -m biotechevents.cli changes --days 7
```

## Architecture

```
biotechevents/
├── biotechevents/
│   ├── store.py                    SQLite + raw_filings + change-tracking
│   ├── scrape_clinicaltrials.py    ClinicalTrials.gov v2 JSON API
│   ├── scrape_fda.py               FDA PDUFA — stubbed for v1
│   ├── normalize.py                phase/status canonicalization, sponsor→ticker
│   └── cli.py
├── tests/
├── hooks/pre-commit                CHANGELOG enforcement
└── data/biotechevents.db           output (gitignored)
```

## What we capture

For every clinical trial registered on ClinicalTrials.gov:

| Field | Why it matters |
|---|---|
| `nct_id` | Unique trial identifier |
| `phase` | Phase 2→3 transition is one of the strongest biotech alpha signals |
| `overall_status` | RECRUITING vs SUSPENDED vs TERMINATED — flips the thesis |
| `primary_completion_date` | When primary endpoint data is expected — drives the next leg |
| `sponsor_name` + `ticker` | Maps to a tradable equity when possible |
| `enrollment_count` | Trial size / power |
| `conditions_json` | Disease(s) — for sector / theme overlays |
| `interventions_json` | Drug or device name(s) |

**Change tracking.** Every time we see a trial whose phase, status, or
primary_completion_date differs from what we have stored, we log a row
to `trial_changes`. Querying that table gives you the actionable feed:
"these trials had Phase 2 → Phase 3 transitions in the last week" or
"these were just terminated."

## Engineering pattern (lifted from congresstrades / edgar13f)

- **Raw before parse** — every API response stored in `raw_filings`
  before any parser runs. Future parser improvements re-process from
  cache, no re-scraping.
- **`parser_version`** on every row.
- **Idempotent migrations** via try/except on ALTER TABLE.
- **Change detection** — `upsert_trial` compares incoming values
  against existing row and writes diff rows to `trial_changes`.
- **Pre-commit hook** blocks `.py` commits without same-day CHANGELOG entry.
- **Polite** — 1 req/sec on the ClinicalTrials API (their docs allow
  more, but we don't need it).
- **User-Agent** with contact email per public-records etiquette.

## Sponsor → ticker mapping

ClinicalTrials.gov sponsor names don't include tickers. We maintain a
hand-curated map of major pharma + biotech in `normalize._SPONSOR_TO_TICKER`,
seeded with ~40 well-known names. Unknown sponsors insert with `ticker=NULL`
— the sponsor name is preserved so queries by sponsor still work.

Extend the map as you encounter sponsors whose tickers matter for your
watchlist.

## Why no FDA scraper yet

See the docstring at the top of `scrape_fda.py`. Short version: FDA
doesn't publish a clean structured PDUFA calendar; third-party
scrapers get killed periodically; paid alternatives violate the
free-infrastructure philosophy. ClinicalTrials.gov phase transitions
are the upstream leading indicator anyway — when a Phase 3 trial
completes, a PDUFA date follows within 6-12 months.

When we tackle FDA later, the hooks are already in place: `pdufa_events`
table exists in the schema, `scrape_pdufa_calendar()` function is wired
into the daily pipeline (currently a stub).

## Testing

```bash
python -m pytest                # all tests
python -m pytest tests/test_normalize.py   # one file
```
