# congresstrades

Local, on-demand scraper for US Congressional stock-trade disclosures
(House PTRs + Senate PTRs). Emits a portable SQLite file that other
tools can ingest.

**Status:** prototype. House scraper works. Senate is stubbed. Press a
button, see results, decide whether to productionize.

## Why this exists

Members of Congress are required by the STOCK Act (2012) to disclose
stock trades within 30-45 days. The disclosures are public. Several
vendors sell access to this data ($20-50/mo retail, $500-2000/mo
enterprise) — but the source sites are free, if inconvenient to scrape.

This project skips the vendor layer and pulls directly from the
authoritative sources:

- **House**: https://disclosures-clerk.house.gov/public_disc/financial-pdfs/
- **Senate**: https://efdsearch.senate.gov/search/

## Quickstart

### 1. Install

```bash
cd /Users/mackr0/congresstrades
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Pull the current year's House data (smoke test first)

```bash
# Limit to 10 filings for a quick sanity check (~30 seconds)
python -m congresstrades.cli refresh --year 2025 --limit 10

# Full year — can take 20-40 min for ~1500 PTRs
python -m congresstrades.cli refresh --year 2025
```

The first run downloads and caches the yearly ZIP (~100-300 MB) to
`data/cache/`. Subsequent runs reuse the cache unless you pass
`--force-zip-refresh`.

### 3. Look at what came in

```bash
# Trade counts by chamber
python -m congresstrades.cli counts

# All recent trades, most-recent first
python -m congresstrades.cli show

# Filter by ticker
python -m congresstrades.cli show --ticker NVDA

# Filter by member (substring match, case insensitive)
python -m congresstrades.cli show --member "Pelosi"

# See history of scrape runs + success/failure
python -m congresstrades.cli runs
```

### 4. Export for external tools

```bash
python -m congresstrades.cli export --format csv --out trades.csv
```

## Architecture

```
congresstrades/
├── congresstrades/          # the package
│   ├── scrape_house.py      # ← primary scraper (works)
│   ├── scrape_senate.py     # ← stubbed
│   ├── normalize.py         # ticker extraction + amount-range parsing
│   ├── store.py             # sqlite schema + CRUD
│   └── cli.py               # click-based CLI
├── data/
│   ├── congress.db          # the output (gitignored)
│   └── cache/               # downloaded ZIPs + PDFs (gitignored)
├── tests/                   # (empty — add as needed)
├── requirements.txt
└── README.md
```

**Zero coupling.** This repo has no knowledge of QuantOpsAI or any other
consumer. If you want to ingest the output somewhere, read `data/congress.db`
directly or use `export`.

## How the House scraper works

1. `_download_year_zip(YEAR)` fetches
   `https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{YEAR}FD.zip`
   which contains:
   - `{YEAR}FD.xml` — index of every filer + filing type + doc_id
   - PDFs of every filing
2. `_parse_index()` extracts all entries where `FilingType='P'` (PTR).
3. For each PTR, download the PDF (cached by doc_id).
4. `pdfplumber` extracts tables. We look for the canonical headers
   (`Asset`, `Transaction Type`, `Date`, `Amount`) and pull matching rows.
5. Each row is normalized:
   - Asset description → ticker (via `normalize.extract_ticker`)
   - Transaction type → `buy` / `sell` / `exchange` / `partial_sale` / `other`
   - Amount range → `(amount_low, amount_high)` integers
6. Insert into sqlite. The UNIQUE constraint on
   `(chamber, member, doc_id, txn_date, asset, amount_range)` makes
   re-runs idempotent.

### Known limits

- **PDF layouts vary.** ~10-15% of PTRs have non-standard layouts and
  silently yield zero rows. Logged at DEBUG. These are acceptable to
  miss for a signal source — the concentrated large trades from
  active-trader members are the ones we care about, and those members
  use the standard format.
- **Party affiliation** is not in the XML index. Look up via a separate
  member roster if needed.
- **Asset descriptions are lossy.** We store the raw text and make a
  best-guess ticker. When uncertain, `ticker` is `NULL` — inspect
  `asset_description` directly. Expand `normalize._NAME_TO_TICKER` as
  you see real patterns fail.

## Why the Senate scraper is a stub

The Senate site (`efdsearch.senate.gov`) requires:
1. A session cookie obtained by POSTing "I agree to terms" before queries.
2. Aggressive rate-limiting (~1 req/sec max before throttle).
3. HTML parsing that changes every 6-12 months.

This is solvable but noisy, and a half-broken scraper that silently
stops importing Senate data is worse than no Senate data. Stub fails
loudly with `status='STUB'` in `scrape_runs`.

See `scrape_senate.py` module docstring for implementation notes when
you're ready to tackle it.

## Expected lifetime

The House site's ZIP format has been stable since ~2018. Low chance of
breaking. The Senate site changes backend every 6-12 months. If this
project is actively maintained, expect:

- ~2-4 hours/year maintenance on the House scraper
- ~5-15 hours/year whenever we get Senate working and need to repair it

## License / legal note

All data pulled is public (STOCK Act disclosures) and the disclosure
sites publish it precisely so it can be used. The terms of service on
`disclosures-clerk.house.gov` and `efdsearch.senate.gov` do not
prohibit automated access of public records, but scrape politely
(cached requests, reasonable rate limits, good User-Agent string).
Don't hammer — there's no need to.
