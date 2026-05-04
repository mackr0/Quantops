"""edgar13f — SEC 13F institutional holdings scraper.

Standalone local-first tool. Reads 13F-HR filings directly from EDGAR
and stores a normalized per-quarter holdings view in a local SQLite.

Public modules:
  store         SQLite schema + CRUD + raw_filings layer
  scrape        EDGAR filings list + XML parser
  normalize     CUSIP / value / metadata canonicalization
  cli           click-based CLI (daily, refresh, show, counts, runs)

See README.md for quickstart.
"""

__version__ = "0.0.1"
