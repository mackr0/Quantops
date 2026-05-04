# Changelog

Every meaningful code change — bugs, behavior changes, features —
gets an entry. Newest at the top.

Rules:
- Pre-commit hook blocks `.py` changes without a same-day entry.
- Emergency bypass: `git commit --no-verify` (document why).

---

## 2026-04-24 — Initial release: 13F-HR scraper + SQLite store + CLI (Severity: feature)

Local-first scraper for SEC 13F-HR institutional holdings filings.
Companion to `congresstrades`, following the same engineering pattern
(raw_filings layer, parser_version tagging, pre-commit hook,
contract tests).

**What works:**
- EDGAR submissions JSON → list of 13F-HR filings per filer
- Informationtable XML fetch + parse
- SQLite store with filers / filings / holdings / raw_filings / scrape_runs
- CLI: `daily`, `refresh`, `show`, `counts`, `filers`, `runs`
- Hand-curated starter roster of 16 notable filers (Berkshire, Renaissance,
  Bridgewater, Citadel, Norges Bank, CalPERS, etc.)

**Smoke test against Berkshire Hathaway:** pulled a 13F-HR filing with
110 positions totaling ~$274B — matches the known real-world portfolio
size. Top positions (AMEX $55B, AAPL $21.9B+$16.7B+$9.4B across share
classes, KO $19.8B, BAC $17.1B, OXY $10.9B, Chubb $10.7B, Kraft Heinz
$7.9B) all match public reporting.

**Bugs fixed during initial build:**

1. **Namespace regex stripped only first xmlns.** 13F XML declares both
   `xmlns:xsi=...` and `xmlns="..."` at root. My `re.sub(..., count=1)`
   removed only the first, leaving the default namespace intact and
   causing ET.findall to return zero elements. Removed the `count=1`
   limit. Regression test in `test_scrape_parser.py` guards this.

2. **Value parser multiplied by 1000.** SEC rule change (FR-86, 2022)
   switched 13F value reporting from thousands-of-dollars to actual
   dollars. My initial parser multiplied by 1000, producing ~$274T
   figures for a $274B portfolio. Fixed by removing the multiplier
   and renaming `parse_value_thousands` → `parse_value_dollars`
   (old name preserved as back-compat alias). Verified against
   Berkshire's Ally Financial position: value=576,074,081 with
   12.7M shares ≈ $45/share stock price.

3. **SEC submissions JSON sometimes has unequal parallel arrays.**
   `form[]`, `periodOfReport[]`, `filingDate[]` etc. are documented
   as parallel but occasionally the period array is shorter. Added
   defensive `_safe(arr, i)` bounds-checking so the scraper doesn't
   IndexError on the missing period — instead it stores an empty
   period_of_report (acceptable — the parser extracts the real period
   from the XML anyway).

**Tests:** 4 test files covering normalize (value/shares/CUSIP/ticker
map), store (schema + CRUD + idempotency), parser (real Berkshire XML
fixture + regression guards), and contract tests (architectural
invariants: User-Agent with email, rate-limit detection, raw-before-
parse ordering, parser_version tagging, UNIQUE constraint allowing
multiple share classes).

**Follows the architecture pattern from ALTDATA_PLAN.md:**
- Pre-commit hook + changelog enforcement
- Separate repo, local-first, private GitHub backup
- SEC-polite (1 req/sec, 10x under their published limit)
- Raw XML persisted to `raw_filings` before parsing; `parser_version`
  tagged on every row; idempotent migrations via try/except on ALTER.
