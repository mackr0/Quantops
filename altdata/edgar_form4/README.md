# edgar_form4

Local, on-demand scraper for SEC **Form 4** filings — statements of
changes in beneficial ownership filed by company insiders (officers,
directors, 10%+ owners) within 2 business days of any transaction.

Companion to `congresstrades` and `edgar13f`. Same standalone
local-first pattern. Replaces the yfinance-backed insider lookups in
`alternative_data.get_insider_activity` + `get_insider_cluster` with
a free, authoritative source.

## What it does

For each tracked ticker, the scraper:

1. Looks up the company's CIK from the local `companies` table
   (seeded weekly from `sec.gov/files/company_tickers.json`).
2. Hits `https://data.sec.gov/submissions/CIK{cik}.json` to list
   recent Form 4 filings.
3. For each new filing, fetches the XML primary document
   (`https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}`).
4. Persists raw XML to `raw_filings` BEFORE parsing.
5. Parses `<nonDerivativeTransaction>` blocks → per-transaction rows.

**Output per insider transaction:**
- Insider identity: name, isOfficer / isDirector / isTenPercent flags, title
- Transaction date + code (P=Purchase, S=Sale, A=Award, etc.)
- Shares + price per share + computed USD value
- Acquired/Disposed code, Direct/Indirect ownership

**Aggregate readout per ticker** (consumed by
`alternative_data.get_insider_form4`):
- `recent_buys`, `recent_sells` (last 90 days, P/S codes only)
- `total_buy_value` + `total_sell_value` USD
- `net_direction` ('buying' / 'selling' / 'neutral')
- `notable` — biggest insider buy with title + amount + date
- `cluster_count` — distinct insiders buying in last 14 days

## Quickstart

```bash
cd /opt/quantopsai/altdata/edgar_form4
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 1. Seed the ticker → CIK map (run once + weekly thereafter)
python -m edgar_form4.cli refresh-tickers

# 2. Refresh one company
python -m edgar_form4.cli refresh --ticker AAPL

# 3. Show aggregate
python -m edgar_form4.cli show --ticker AAPL

# 4. Daily batch (called from altdata/run-altdata-daily.sh)
python -m edgar_form4.cli daily --tickers AAPL,MSFT,JPM,...
```

## Architecture

- `store.py` — SQLite schema + CRUD + raw_filings + scrape_runs.
- `scrape.py` — EDGAR submissions JSON + Form 4 XML fetch.
- `normalize.py` — Form 4 XML parser, transaction-code semantics.
- `cli.py` — click-based CLI.

DB at `data/edgar_form4.db`. ~10MB for a 200-ticker universe with
6 months of history. Idempotent — re-running the same scrape is a
no-op (dedup'd via UNIQUE constraints).

## Rate limits

SEC publishes 10 req/sec; we throttle to 1 req/sec to be polite (no
ETag / If-Modified-Since negotiation yet — every filings-list call
costs one round trip).

## What it does NOT do (yet)

- Derivative transactions (`<derivativeTable>`) — Form 4 also discloses
  options grants / exercises. Currently parsed-only-but-not-stored;
  add when a strategy actually needs them.
- Insider amendments (Form 4/A) — we treat them like fresh filings;
  amendment-vs-original linkage isn't tracked.
- Filings older than `max_age_days` (default 90) on initial scrape.
  For backfill, run with `--max-age-days 365`.
