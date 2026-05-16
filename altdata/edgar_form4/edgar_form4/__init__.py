"""edgar_form4 — SEC Form 4 (insider transactions) scraper.

Standalone local-first tool. Reads Form 4 filings — statements of
changes in beneficial ownership filed by company insiders (officers,
directors, 10%+ owners) within 2 business days of any transaction —
directly from SEC EDGAR and stores a normalized per-transaction view
in a local SQLite. Replaces the yfinance-backed insider lookups in
`alternative_data.get_insider_activity` + `get_insider_cluster` with
a free, authoritative source.

Companion to `edgar13f` and `congresstrades`. Same standalone
local-first pattern (no scheduler, no daemon, just CLI).

Public modules:
  store       SQLite schema + CRUD + raw_filings layer
  scrape      EDGAR submissions JSON + Form 4 XML fetch
  normalize   transaction-code translation, ticker/CIK mapping
  cli         click-based CLI (daily, refresh, show, counts, runs)

Public API consumed by the QuantOpsAI trade pipeline (via
`alternative_data.get_insider_form4(symbol)`):
    {
        recent_buys: int (count in last 90 days, transaction code P)
        recent_sells: int (count in last 90 days, code S)
        net_direction: "buying" | "selling" | "neutral"
        notable: str or None (e.g., "CEO bought $2.1M on Apr 5")
        total_buy_value: float
        total_sell_value: float
        cluster_count: int (distinct insiders buying in last 14d)
    }

See README.md for quickstart.
"""

__version__ = "0.1.0"
