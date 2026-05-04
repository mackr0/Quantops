# Changelog

Every meaningful code change — bug fixes, behavior changes, new features —
gets an entry. Newest at the top.

**Format:** `YYYY-MM-DD — short title. Severity/Type.`

**Enforcement:** a pre-commit hook blocks commits that modify any `.py`
file without updating this file (matching today's date). Install via
`./scripts/install-hooks.sh`. The matching `tests/test_changelog_enforcement.py`
verifies the hook script itself hasn't drifted.

Rules:
- Every production-relevant behavior change needs an entry here before commit
- Every fix names the regression test (or TODO to add one)
- Be honest about *what broke, why it wasn't caught, what the fix does*

---

## 2026-04-24 — `daily` + `prices` CLI: one-button refresh workflow (Severity: feature)

New `prices.py` module + two CLI commands to make the daily refresh
routine a single command.

**`daily` — the one-button workflow:**

```
python -m congresstrades.cli daily
```

Runs House → Senate → Prices in order. Force-refreshes the House
yearly ZIP (current year gets new filings daily). Rate-limited
throughout (House 0.4 req/sec, Senate 2 req/sec, Prices 1 req/sec).
~15-25 min total. Reports row-count deltas so you can see exactly
what was new this run.

Options:
  `--year` (default: current calendar year)
  `--skip-prices` — fast mode, ~5 min
  `--force-prices` — re-fetch every ticker (wastes Yahoo quota, use sparingly)

**`prices` — standalone price cache refresh:**

```
python -m congresstrades.cli prices                  # tickers active this year
python -m congresstrades.cli prices --chamber senate
python -m congresstrades.cli prices --period 3y      # extend for multi-year P&L
python -m congresstrades.cli prices --all --force    # nuke + rebuild
```

Needed before `pnl` runs if you want marked-to-market accuracy.
Skip-if-cached by default; re-runs are free.

**`prices.py` module design:**

Pure-function interior: `refresh_prices(tickers, fetcher=..., delay=..., ...)`
takes an injected fetcher callable so tests never hit Yahoo. Stats
returned: `fetched / cached / empty / errors`. Progress callback for UI.
Cache sentinel: empty response writes an empty CSV so next run
doesn't re-fetch delisted tickers every time.

**Contract tests:** `REQUEST_DELAY_SEC >= 1.0` — a drop below risks
Yahoo rate-limiting. The number is what we validated at the ~600-ticker
bulk-fetch earlier today.

Tests: 111 → 125 passing. 14 new in `test_prices.py`: cache hit/miss,
force-refetch, empty-sentinel, exception-tolerance, progress callback,
DB-driven ticker discovery with year/chamber filtering.

---

## 2026-04-24 — P&L estimator: FIFO lot matching + range-midpoint dollar bounds (Severity: feature)

New `pnl.py` module + `cli pnl` command. Answers "how much did each
member make?" as precisely as the underlying STOCK Act disclosures allow
— which is to say, as point-estimate dollar P&L with uncertainty bounds
derived from the amount-range disclosures.

**Algorithm:**

1. Per member, per ticker, trades sorted by date.
2. BUYs push onto a FIFO queue with `[amount_low, amount_high]` and the
   closing price on buy date.
3. SELLs and PARTIAL_SALEs pop from the oldest lot first (FIFO). The
   consumed slice becomes a closed `Roundtrip` with return_pct from
   market price movement between buy_date and sell_date.
4. Unmatched buys at end-of-window become `OpenPosition`s marked-to-
   market against the latest cached close.
5. EXCHANGEs and other types skip cleanly without creating phantom
   positions.

**Uncertainty model:**

Dollar P&L for a trade is approximately `position_size × return_pct`.
Position size is constrained to `[amount_low, amount_high]`. We emit
three numbers per trade: low bound, midpoint estimate, high bound.
When return is negative, the bounds flip in dollar terms (bigger
position = bigger loss). The CLI `pnl` command shows all three with
the midpoint highlighted.

Aggregated across a member's trades, the midpoint converges toward
the true value (positive and negative range errors partially cancel),
but absolute bounds widen. The CLI shows both.

**CLI:** `python -m congresstrades.cli pnl [--chamber] [--year] [--member] [--min-trades] [--top]`
- Leaderboard mode (default) shows top N members by total midpoint P&L
- Single-member mode (`--member "Pelosi"`) shows per-position detail

Tests: 98 → 111 passing. 13 new tests in `test_pnl.py` cover FIFO
ordering, partial sales, exchange skipping, loss-bound inversion,
price-unavailable graceful degradation, cross-ticker aggregation,
and closed-only win-rate computation.

**Preview of real 2025 House results (min 10 trades):**
- Tim Moore: ~$1.0M mid (mostly unrealized; wide band $460K-$1.6M)
- Gilbert Cisneros: ~$860K mid ($413K realized + $447K unrealized)
- Suzan DelBene: ~$485K mid (the 2025 "never lost" standout)
- Nancy Pelosi: $348K realized (mostly closed — tight band)
- All bounds reflect STOCK Act range disclosures — not our scraper's
  precision limit.

---

## 2026-04-24 — Testing + commit discipline: pre-commit hook, 98 tests (Severity: infrastructure)

Ported the QuantOpsAI discipline to congresstrades:

**Pre-commit hook** (`hooks/pre-commit`, installed via
`scripts/install-hooks.sh`) — blocks commits modifying any `.py` file
without a same-day entry in `CHANGELOG.md`. Bypass with `--no-verify`
for emergencies. Shell-only (`bash -n` validates syntax).

**Test suite** — 98 tests across 6 files, all hermetic (no network):

- `test_normalize.py` (28) — ticker extraction / amount parser / tx-type
  canonicalization. Guards the 2026-04-24 false-positive fix (IRA, CRT,
  ROTH, etc. must stay blocked).
- `test_store.py` (17) — schema, idempotent migrations, insert_trade
  dedup, raw_filings upsert + blob/text routing, parse_status lifecycle.
- `test_senate_parser.py` (13) — uses a real Senate PTR HTML fixture
  (James Banks, 04/20/2026) to verify the column-map parser. Layout
  drift will break tests before it breaks production.
- `test_house_parser.py` (14) — synthetic pdfplumber-shaped table
  matrices exercise continuation-row merging, noise filtering, header
  detection in rows 0/1/2.
- `test_contracts.py` (17) — source-inspection guards for architectural
  invariants: PTR URL path, rate-limit detection, parser_version
  tagging, raw-before-parse ordering, IRA/CRT blocklist presence.
- `test_changelog_enforcement.py` (13) — the meta-test. Hook exists,
  is executable, filters `.py`, checks today's date, passes `bash -n`.
  If the discipline machinery breaks, this catches it.

**Parser hardening** (discovered via tests):

- House `_parse_table`: orphan continuation rows (asset text with no
  transaction fields, appearing before any real trade) now drop
  cleanly instead of becoming ghost trades with empty Type/Date/Amount.
  Minor tightening of the 2026-04-24 continuation fix.
- Senate `_clean_member_name`: collapses whitespace FIRST before
  matching "The Honorable" so multi-space HTML artifacts don't defeat
  the honorific stripper.

**Tests: 0 → 98 passing.**

---

## 2026-04-24 — Senate PTR scraper + raw_filings storage + parser_version tagging (Severity: feature)

End-to-end Senate scraping, previously stubbed. Session flow
(agreement POST → search-page CSRF → DataTables search → individual
filing HTML) is working; 5-filing smoke test pulled 30 clean trades
across 4 senators with zero parse errors.

**Future-proof storage architecture:**

1. `raw_filings` table stores every fetched document (HTML/JSON/PDF)
   alongside metadata. If Senate layout changes in 6 months and the
   parser breaks, we re-run parsing against the cached HTML — no
   re-scraping the origin site.
2. `trades.parser_version` tags every row with the parser that produced it
   (`'senate-html-v1'`). Parser improvements can re-process historical
   rows selectively.
3. Idempotent migration — `_apply_migrations()` adds new columns via
   try/except on `ALTER TABLE`. Safe to run against existing DBs.
4. `parse_status` lifecycle on `raw_filings`: `'unparsed'` → `'parsed'`
   or `'parse_error'`. Paper-PDF filings stay `'unparsed'` so a future
   PDF parser can pick them up without re-fetching.

**Senate scraper design:**
- `requests.Session` persisting cookies across agreement + search
- 2s delay between requests (Senate is pickier than House)
- Monthly date-range chunking so DataTables 100-row cap is never hit
- HTML parser uses column-map lookup so small layout tweaks don't break
  parsing — only header renames would
- Ticker from "Ticker" column directly; `extract_ticker()` only for
  rows where it's missing/junk

CLI updated: `--senate` now takes `--year` like `--house`.

## 2026-04-24 — Expand _NON_TICKER_WORDS based on real disclosure data (Severity: fix)

First full-year House pull surfaced false-positive ticker matches from
the fallback bare-uppercase regex:
- `IRA` (499 matches) — Individual Retirement Account
- `CRT` (309 matches) — Charitable Remainder Trust
- `OT` (61 matches) — Other Transactions
- Plus `ROTH`, `SEP`, `HSA`, `GRAT`, `REIT`, `FHA`, `GNMA`, `FNMA`, etc.

All appear in disclosure free-text like "Fidelity IRA #XYZ holding cash"
and were being emitted as tickers. Expanded the blocklist:
- Account types (IRA, ROTH, SEP, HSA, CRT, GRAT, GRT)
- Transaction abbreviations (OT, PUR, SALE, BUY, SELL, DIV, INT)
- Legal structure / instrument types (LTD, TRUST, ACCT, JT, SPS, DC,
  CD, CDS, MMF, REIT, FHA, GNMA, FNMA)

Re-parsing the cached 2025 PDFs after this commit dropped false
positives completely — ticker coverage went 86.1% → 96.4% and total
"trades" went 7,514 → 3,920 (the drop was ghost rows eliminated by
the continuation-row merge, see next entry).

## 2026-04-24 — Parser: merge continuation rows + commit per filing (Severity: fix)

Two robustness improvements:

1. **Continuation-row handling.** pdfplumber sometimes splits a logical
   trade into two rows when the asset name wraps. Previously we created
   a ghost record for the continuation with empty Type/Date/Amount
   (those `—` rows in the smoke test). Now: if a row has asset text
   AND none of the three key transaction fields (type, date, amount),
   merge its text into the previous trade's `asset_description`.

2. **Per-filing commit.** Previously we committed only at the end of
   the whole year's run, so a mid-run crash or rate-limit lost ALL
   inserted trades even though PDFs remained cached. Now commit after
   each filing's trades are inserted. Trivial overhead, much better
   crash-resilience: a re-run after failure resumes exactly where
   the last successful filing left off.

## 2026-04-24 — Add politeness throttle + 429/403 detection (Severity: robustness)

Before full-year pulls and multi-year backfills, added:
- 0.4s delay between PDF downloads (~2.5 req/sec max, well below any
  reasonable gov-site rate limit)
- Explicit `RateLimitedError` on HTTP 429/403 bubbles up to abort the
  run cleanly instead of silently failing 400 PDFs in a row
- Cache-hit path skips the throttle (free for already-downloaded PDFs)

Full-year pull of 515 PTRs now takes ~4 min of request time + parse
overhead. On rate-limit: re-run picks up from local cache + DB dedup,
so losing a few hours to a block is annoying but not destructive.

## 2026-04-24 — Fix: PTR PDFs live at ptr-pdfs/YYYY/, not financial-pdfs/YYYY/ (Severity: fix)

Discovered during first real refresh: the House clerk splits the URL
namespace between the yearly ZIP index (at `financial-pdfs/`, correct)
and individual PTR PDFs (at `ptr-pdfs/`, previously wrong). All 5
smoke-test PDFs returned 404 before the fix; now 3/5 successfully
extracted 12 trades.

Added a new `PTR_PDF_URL` constant next to `BASE_URL` with a comment
explaining why the two paths diverge. Other filing types (annual PFDs,
amendments) live on yet other paths — we'll add those when we extend
beyond PTRs.

## 2026-04-24 — Initial skeleton: House scraper + sqlite store + CLI + Senate stub (Severity: feature)

Local, on-demand scraper for US Congressional PTR disclosures. Prototype
running locally, validates the approach before productionization.

**What works:** House scraper (yearly FD zip + PTR PDFs via pdfplumber),
SQLite store with idempotent UNIQUE constraints, hand-curated name-to-ticker
map, amount-range parser, transaction-type canonicalization, Click-based
CLI (`refresh`, `show`, `counts`, `runs`, `export`).

**What's stubbed:** Senate scraper. JS-gated session flow + periodic HTML
layout changes make it worse-than-nothing to ship half-working. Stub
fails loudly (logs STUB status in `scrape_runs`) rather than silently
inserting empty data.

**Design principles:** zero coupling to any consumer (standalone repo),
append-only storage with raw text preserved even when ticker unknown,
cached yearly ZIP + per-PDF cache in `data/cache/` (gitignored),
ticker NULL when genuinely uncertain (better than wrong guesses).
