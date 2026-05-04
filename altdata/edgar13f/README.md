# edgar13f

Local, on-demand scraper for SEC 13F-HR institutional holdings. Pulls
directly from EDGAR (free, authoritative) and stores a normalized
per-quarter holdings view in SQLite.

Companion to `congresstrades`. Both are standalone local-first tools
following the same pattern. See `ALTDATA_PLAN.md` in the QuantOpsAI
repo for the architectural context.

## What it does

13F-HR filings are required quarterly from every investment manager
with >$100M AUM (Berkshire Hathaway, Renaissance, Bridgewater, Citadel,
sovereign funds, state pension systems, etc.). They're due 45 days after
quarter-end. This scraper pulls the XML directly from EDGAR's official
`submissions` JSON + Archives path.

**Output per filing:**
- Who the filer is (CIK + name)
- What quarter it covers (`period_of_report`)
- Every position: CUSIP, ticker (where mapped), company name, shares,
  USD value, put/call, investment discretion

## Quickstart

```bash
cd /Users/mackr0/edgar13f
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Install the commit discipline hook
./scripts/install-hooks.sh

# Smoke test: 2 filings for one filer (Berkshire)
python -m edgar13f.cli refresh --cik 0001067983 --max-filings 2

# See what landed
python -m edgar13f.cli counts
python -m edgar13f.cli show --limit 20
python -m edgar13f.cli show --ticker AAPL

# Full daily refresh — all configured filers
python -m edgar13f.cli daily
```

Total time for first full run: ~10-20 minutes (16 filers × ~5 recent
filings each × 1 req/sec politeness).

## Commands

| Command | Purpose |
|---|---|
| `daily` | Refresh every filer in the starter roster |
| `refresh --cik ...` | Refresh one filer |
| `show [--ticker ...] [--cusip ...]` | Query current holdings |
| `counts` | Filings per quarter |
| `filers` | List configured filers |
| `runs` | Scrape-run history |

## Architecture

```
edgar13f/
├── edgar13f/
│   ├── store.py         SQLite schema + CRUD + raw_filings layer
│   ├── scrape.py        EDGAR + XML parser
│   ├── normalize.py     CUSIP validation, value parsing, ticker map
│   └── cli.py
├── tests/               pytest suite
├── hooks/pre-commit     blocks .py commits without CHANGELOG update
└── data/edgar13f.db     output (gitignored)
```

## Key design choices

**Future-proof storage.** Every fetched XML is saved to `raw_filings`
before any parser runs. If SEC changes the filing format or we improve
the parser, we re-parse from the cache — no re-scraping.

**`parser_version` on every row.** A parser upgrade can re-process
specific historical rows.

**Idempotent.** UNIQUE constraints on `filings` and `holdings` mean
re-running the same day inserts nothing; only genuinely new disclosures
get added.

**SEC-polite.** 1 req/sec (vs their 10 req/sec ceiling). Proper
User-Agent including contact email (their docs require this).

**Pre-commit hook.** `.py` commits blocked without a same-day
`CHANGELOG.md` entry. Bypass: `git commit --no-verify`.

## CUSIP → ticker

13F filings identify securities by CUSIP, not ticker. There's no free
authoritative CUSIP→ticker map. We maintain a hand-curated seed of
well-known CUSIPs in `normalize._CUSIP_TO_TICKER`. Unknown CUSIPs
insert with `ticker=NULL` — the CUSIP + company name are still stored
so queries can work without ticker.

Extend the map as you encounter real holdings whose tickers matter.

## Starter filer roster

See `FILERS` in `scrape.py` for the initial ~16 names. Add a CIK + name
+ type to extend. CIKs can be looked up at
`https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany`.

## Known limitations

- Only scrapes **13F-HR** (holdings). 13F-NT (no-holdings reports) are
  skipped.
- `ticker` is best-effort; rely on `cusip` for authoritative matching.
- Value is reported to the nearest **thousand dollars** by filers;
  we multiply ×1000 to store actual dollars.
- We don't parse 13F amendments (13F-HR/A) specially — they come in
  as normal filings and the UNIQUE constraint handles dedup.

## Next steps (not built yet)

- CUSIP→ticker discovery via cross-reference with other sources
  (congresstrades has tickers, can reverse-map)
- 13D/13G filings (activist stakes > 5%) as a follow-on project
- Quarterly-change analysis: "fund X added 500K shares of NVDA this
  quarter" — requires diffing consecutive filings per filer
