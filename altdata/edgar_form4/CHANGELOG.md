# Changelog — edgar_form4

## 0.1.0 — 2026-05-16

Initial release. Replaces the yfinance-backed insider lookups in the
parent QuantOpsAI codebase (`alternative_data.get_insider_activity`
and `get_insider_cluster`) with a direct SEC EDGAR Form 4 source.

**Scope of v0.1.0:**
- Ticker → CIK seeding from `sec.gov/files/company_tickers.json`
- Per-CIK listing of Form 4 filings (last 90 days by default)
- XML parsing of non-derivative transactions
- Storage: companies, form4_filings, insider_txns, raw_filings, scrape_runs
- Aggregate reader `get_recent_insider_activity(ticker)` that matches
  the legacy interface (recent_buys/sells/values/cluster_count)
- CLI: refresh-tickers, refresh, daily, show, counts, runs

**Out of scope for v0.1.0** (documented in README):
- Derivative transactions (options grants/exercises)
- Form 4/A amendments treated as fresh filings
- Backfills > 90 days require explicit `--max-age-days`
