# Alt-Data Expansion Plan

**Purpose:** Build 3 new standalone data-source projects over the next 6 months
that add unique signal to QuantOpsAI's 15-signal pipeline, following the
`congresstrades` pattern (local-first, git-backed, one-button `daily`,
strict engineering discipline).

**Principle:** Each project is independent — its own repo, its own SQLite,
its own CLI, its own tests. QuantOpsAI consumes their outputs later via
well-defined schemas. No tight coupling.

**Last updated:** 2026-04-24. Status: Project 1 in progress.

---

## Why these three (and not others)

| # | Project | Primary signal | Effort | Unique vs existing |
|---|---|---|---|---|
| 1 | **edgar13f** | Quarterly institutional holdings — who OWNS what | 2-3 days | Complements Form 4 (who bought/sold) |
| 2 | **biotechevents** | FDA PDUFA calendar + clinical trial milestones | 2-3 days | Nothing in existing pipeline covers this |
| 3 | **stocktwits** | Retail sentiment via StockTwits API | 1 day | Pairs with Reddit (pending app approval) |

Rejected (noted for posterity, don't revisit without a reason):

- **Twitter/X sentiment** — $42K/year API paywall makes it non-viable for a free-infrastructure stack.
- **Satellite imagery / credit card data** — all paid tier, violates the free-infrastructure philosophy.
- **USPTO patent velocity** — the v2 API is currently broken (per 2026-04-23 CHANGELOG). Revisit when stable.
- **Job postings / layoff tracker** — signal is too noisy at the individual-stock level.

---

## The shared pattern (lifted from `congresstrades`)

Every project in this plan inherits this structure. When something in here
drifts, all projects drift together — update this document, not individual
repos.

### Repo layout

```
{project}/
├── CHANGELOG.md               # enforced — pre-commit hook blocks .py without update
├── README.md                  # quickstart + daily routine + known-limits section
├── .gitignore                 # venv, *.db, cache/
├── pytest.ini                 # test config
├── requirements.txt
├── hooks/
│   └── pre-commit             # symlinked into .git/hooks/ by install script
├── scripts/
│   └── install-hooks.sh
├── {project}/                 # Python package
│   ├── __init__.py
│   ├── store.py               # SQLite: primary tables + raw_filings layer
│   ├── scrape_*.py            # one per external source
│   ├── normalize.py           # parsers, ticker/date/amount canonicalization
│   └── cli.py                 # click-based: daily, refresh, show, export, counts, runs
├── tests/
│   ├── __init__.py
│   ├── fixtures/              # real-sample HTML/XML/JSON for parser tests
│   ├── test_store.py          # schema + CRUD + migrations
│   ├── test_normalize.py      # pure-function tests
│   ├── test_scrape_*.py       # parser tests using fixtures
│   ├── test_contracts.py      # source-inspection invariants
│   └── test_changelog_enforcement.py   # meta-tests on the hook
└── data/
    └── .gitkeep               # DB file + cache/ are gitignored
```

### Engineering invariants (enforced by tests)

1. **Raw before parse.** Every fetched document (HTML/JSON/XML/PDF) is
   persisted into `raw_filings` BEFORE any parser runs. If parsing crashes
   or the parser changes in 6 months, we re-parse from the cache — no
   re-scraping the origin.

2. **`parser_version` tag.** Every parsed row carries a version string
   (e.g. `'13f-xml-v1'`). When a parser improves, you can identify
   historical rows to re-process.

3. **Politeness delays baked in.** Each source has a `REQUEST_DELAY_SEC`
   module-level constant that contract tests enforce >= the safe minimum.

4. **Rate-limit detection.** `RateLimitedError` raised on HTTP 429/403,
   bubbles up to abort the run cleanly. Better to fail loudly than to
   silently skip hundreds of documents.

5. **Idempotent migrations.** Schema changes happen via `try/except` on
   ALTER TABLE so re-runs against an already-migrated DB are safe.

6. **Per-filing commit.** Scrapers commit after each filing's rows are
   inserted — a mid-run crash never loses completed work.

7. **CHANGELOG enforcement.** Pre-commit hook blocks any `.py` change
   without a same-day CHANGELOG entry. Emergency bypass: `--no-verify`.

### CLI pattern (every project)

```bash
python -m {project}.cli daily         # one-button refresh — the main thing
python -m {project}.cli refresh       # individual manual refresh (finer control)
python -m {project}.cli show          # query current DB contents
python -m {project}.cli counts        # aggregate counts
python -m {project}.cli runs          # recent scrape-run history
python -m {project}.cli export        # CSV / JSON export
```

### Deployment

- Local only. No droplet, no cloud.
- Private GitHub repo at `github.com/mackr0/{project}` (meteor insurance).
- Run from laptop on demand.
- No automated cron — user triggers `daily` when they want fresh data.

---

## Project 1 — edgar13f (SEC institutional holdings)

### Data source
- **URL base:** `https://www.sec.gov/cgi-bin/browse-edgar`
- **Form type:** `13F-HR` (quarterly holdings, due 45 days after quarter-end)
- **What's required:** Every investment manager with >$100M AUM
- **Format:** XML (post-2013), has standardized `informationTable` schema
- **Free:** Yes — it's required by law to be public
- **Rate limit:** SEC publishes 10 req/sec; we'll use 1-2 req/sec

### What it captures
- Per (filer, quarter, security): shares held, market value USD,
  voting authority (sole/shared/none), derivative type (call/put/none)
- Filers include Berkshire Hathaway, Renaissance, Bridgewater, state
  pension funds, Norway sovereign wealth, major banks' asset mgmt arms

### Primary schema

```sql
CREATE TABLE filers (
    cik             TEXT PRIMARY KEY,     -- 10-digit SEC identifier
    name            TEXT NOT NULL,
    aum_usd         INTEGER,              -- latest observed AUM
    filer_type      TEXT,                 -- 'hedge_fund' | 'pension' | 'insurance' | 'bank_am' | 'sovereign' | 'other'
    first_seen      TEXT NOT NULL
);

CREATE TABLE filings (
    accession_number    TEXT PRIMARY KEY,
    cik                 TEXT NOT NULL,
    period_of_report    TEXT NOT NULL,     -- '2025-09-30' etc. (quarter-end)
    filed_date          TEXT NOT NULL,
    total_value_usd     INTEGER,
    total_positions     INTEGER,
    parser_version      TEXT,
    fetched_at          TEXT NOT NULL,
    FOREIGN KEY (cik) REFERENCES filers(cik)
);

CREATE TABLE holdings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    accession_number TEXT NOT NULL,
    cusip           TEXT NOT NULL,        -- 9-char CUSIP
    ticker          TEXT,                 -- derived, best-effort
    company_name    TEXT NOT NULL,
    class_title     TEXT,                 -- 'COM', 'CLASS A', 'PUT', etc.
    shares          INTEGER,
    value_usd       INTEGER,              -- thousands in filing, stored as actual dollars
    put_call        TEXT,                 -- 'PUT' | 'CALL' | NULL
    investment_discretion TEXT,            -- 'SOLE' | 'SHARED' | 'DFND'
    FOREIGN KEY (accession_number) REFERENCES filings(accession_number)
);

CREATE INDEX idx_holdings_cusip ON holdings(cusip);
CREATE INDEX idx_holdings_ticker ON holdings(ticker);
CREATE INDEX idx_filings_period ON filings(period_of_report);
```

Plus the standard `raw_filings` and `scrape_runs` tables.

### CUSIP → ticker mapping problem
- 13F filings use CUSIP (9-char instrument identifier), not ticker.
- There's no free authoritative mapping. Options:
  - Cross-reference with known tickers from existing trades (we already
    have ~1500 unique tickers across QuantOps + congresstrades)
  - Fuzzy-match company names from 13F entries against a starter list
  - Accept NULL ticker when uncertain (96% of sources we care about
    have common CUSIPs)
- Store the raw CUSIP always; ticker is a convenience derivation.

### Scraper design

1. **Filer discovery.** Start with a hand-curated list of ~50 top filers
   (Berkshire, Buffett's partnerships, Renaissance, Bridgewater, Citadel,
   Millennium, AQR, CalPERS, Norges Bank, BlackRock, Vanguard).
   Future: dynamic top-N by AUM.

2. **Per filer: list recent 13F-HR filings.**
   - EDGAR browse URL: `/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=13F-HR`
   - Parse list page for accession_numbers + filing dates

3. **Per filing: fetch the primary `informationTable.xml`.**
   - Cache locally — filings are immutable once filed
   - Parse with `xml.etree.ElementTree`, extract holdings

4. **Normalize + insert.**
   - Value is reported in THOUSANDS — multiply by 1000
   - CUSIP → ticker via our internal map (seed with known names)
   - Idempotent on (accession_number, cusip)

### Integration with QuantOpsAI (future — months away)
- Per-candidate feature: `n_top_funds_holding`, `top_fund_new_adds_this_quarter`
- "Smart money concentration score": weighted sum of top-50-fund holdings
- "New-position flag": fund added this stock for the first time in last quarter
- Feeds into `alternative_data.py` as a new alt-data source

### Effort
- Scaffolding (match congresstrades pattern): 2 hours
- EDGAR search + XML parser: 4-6 hours
- Tests (unit + contract + changelog): 2-3 hours
- Backfill 8 quarters for top 50 filers: ~30 min runtime, one-shot
- **Total: 2-3 days of active work**

---

## Project 2 — biotechevents (FDA + ClinicalTrials.gov)

### Data sources

**FDA PDUFA calendar**
- **URL:** FDA publishes partial data, but BioPharmCatalyst + Drugs.com
  maintain more complete lists
- **Primary:** Scrape `https://www.biopharmcatalyst.com/calendars/pdufa-calendar`
  (free, public, rate-polite source)
- **Fallback:** Direct FDA AdCom schedule pages

**ClinicalTrials.gov API**
- **URL:** `https://clinicaltrials.gov/api/v2/studies` (structured JSON API, free)
- **Rate limit:** 3 req/sec documented
- **Data:** Every trial's status, phase, sponsor, primary completion date

### What it captures
- Upcoming FDA decision dates (PDUFA dates)
- AdCom meetings and their outcomes
- Drug approvals / CRL (rejection) announcements
- Clinical trial phase transitions (Phase 1→2→3)
- Primary completion dates (trigger for likely imminent data readout)

### Primary schema

```sql
CREATE TABLE pdufa_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    drug_name       TEXT NOT NULL,
    sponsor_company TEXT NOT NULL,
    ticker          TEXT,
    pdufa_date      TEXT NOT NULL,        -- YYYY-MM-DD, decision deadline
    action_type     TEXT,                 -- 'NDA' | 'BLA' | 'sNDA' | etc.
    indication      TEXT,                 -- the disease it treats
    outcome         TEXT DEFAULT 'pending', -- 'pending' | 'approved' | 'crl' | 'withdrawn'
    outcome_date    TEXT,
    source_url      TEXT,
    fetched_at      TEXT NOT NULL,
    parser_version  TEXT,
    UNIQUE (drug_name, sponsor_company, pdufa_date)
);

CREATE TABLE clinical_trials (
    nct_id          TEXT PRIMARY KEY,      -- NCTxxxxxxxx
    brief_title     TEXT NOT NULL,
    sponsor         TEXT NOT NULL,
    tickers_json    TEXT,                  -- JSON array; often multiple
    phase           TEXT,                  -- 'PHASE1' | 'PHASE2' | 'PHASE3' | etc.
    status          TEXT,                  -- 'RECRUITING' | 'ACTIVE' | 'COMPLETED' | 'TERMINATED'
    primary_completion_date TEXT,
    last_updated    TEXT,
    conditions_json TEXT,
    fetched_at      TEXT NOT NULL,
    parser_version  TEXT
);

CREATE TABLE adcom_meetings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_date    TEXT NOT NULL,
    committee       TEXT NOT NULL,        -- 'Oncologic Drugs Advisory Committee' etc.
    drug_name       TEXT,
    sponsor_company TEXT,
    ticker          TEXT,
    vote            TEXT,                 -- 'yes' | 'no' | 'mixed' | NULL (if future)
    source_url      TEXT,
    fetched_at      TEXT NOT NULL
);
```

### Integration with QuantOpsAI (future)
- Pre-filter: block new long entries 1-2 days before PDUFA
- Post-approval-drift signal generator
- Clinical Phase 2→3 transitions as event-driven scan trigger

### Effort: 2-3 days

---

## Project 3 — stocktwits (retail sentiment)

### Data source
- **API:** `https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json`
- **Rate limit:** 200 req/hour unauth, 400/hour with registered app
- **Free tier:** sufficient for our use case (not real-time every second)

### What it captures
- Recent messages per ticker (last 30 per request)
- Per-message: body text, sentiment tag (bullish/bearish/none), user, likes
- "Trending tickers" endpoint (what retail is buzzing about)

### Primary schema

```sql
CREATE TABLE messages (
    id              INTEGER PRIMARY KEY,   -- StockTwits msg ID
    ticker          TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    user_name       TEXT,
    body            TEXT NOT NULL,
    sentiment       TEXT,                  -- 'bullish' | 'bearish' | NULL
    created_at      TEXT NOT NULL,
    like_count      INTEGER DEFAULT 0,
    fetched_at      TEXT NOT NULL
);

CREATE INDEX idx_msg_ticker_date ON messages(ticker, created_at DESC);

CREATE TABLE ticker_sentiment_daily (
    ticker          TEXT NOT NULL,
    date            TEXT NOT NULL,
    n_messages      INTEGER NOT NULL,
    n_bullish       INTEGER NOT NULL,
    n_bearish       INTEGER NOT NULL,
    net_sentiment   REAL,                  -- (bullish - bearish) / total
    PRIMARY KEY (ticker, date)
);

CREATE TABLE trending_snapshots (
    snapshot_at     TEXT NOT NULL,
    rank            INTEGER NOT NULL,
    ticker          TEXT NOT NULL,
    message_volume  INTEGER,
    PRIMARY KEY (snapshot_at, rank)
);
```

### Polling strategy
- `daily` command pulls last 24 hours of messages for a watchlist
  (intersection with currently-traded tickers in QuantOpsAI's universe)
- Trending snapshot captured hourly if run as a long-lived polling loop
  (optional, v2 feature)

### Integration with QuantOpsAI (future)
- Per-candidate: 24h net sentiment, message volume change
- Anomaly detection: `vol_today > 10x avg` → flag for investigation
- Complementary to Reddit once that API key arrives

### Effort: 1 day (API is clean JSON)

---

## Orchestration — the "single run update" across projects

After all 3 projects exist + congresstrades, a simple shell script at
`~/run-altdata-daily.sh` chains them:

```bash
#!/usr/bin/env bash
# One command to refresh all local alt-data sources.
# Runs sequentially (not parallel) so each project honors its own rate limits
# without contention.

set -e

PROJECTS=(
    "congresstrades"
    "edgar13f"
    "biotechevents"
    "stocktwits"
)

for proj in "${PROJECTS[@]}"; do
    echo ""
    echo "=========================================="
    echo " $proj"
    echo "=========================================="
    cd "$HOME/$proj"
    source venv/bin/activate
    # The package name may differ from the repo (e.g. biotechevents -> biotechevents)
    python -m "${proj}".cli daily || echo "  !! $proj failed, continuing to next"
    deactivate
done

echo ""
echo "All alt-data sources refreshed."
```

Deferred until all 3 new projects exist.

---

## Cross-session handoff

**If a future session picks up this plan:**

1. Read the "shared pattern" section — every project conforms.
2. Check `~/{project}/CHANGELOG.md` for each project's current state.
3. `git log` in each project for recent commits.
4. Running tests: `cd ~/{project} && source venv/bin/activate && python -m pytest`.
5. Don't skip the pre-commit hook discipline — running `./scripts/install-hooks.sh`
   after cloning is the first thing.

**Current status at plan creation (2026-04-24):**
- `congresstrades`: COMPLETE — 125 tests, 10,500 trades, 4-year history.
- `edgar13f`: IN PROGRESS (this session).
- `biotechevents`: NOT STARTED.
- `stocktwits`: NOT STARTED.
