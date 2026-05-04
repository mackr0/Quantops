"""Congresstrades — public-disclosure scraper for Congressional/Senate PTRs.

Standalone package. No coupling to any other system. Outputs a sqlite
file at `data/congress.db` that can be queried, exported, or ingested
by a downstream consumer.

Public modules:
  scrape_house   House PTR scraper (yearly XML index + PDF parsing)
  scrape_senate  Senate PTR scraper stub (JS-gated; needs session flow)
  normalize      Free-text → ticker mapping, amount-range parsing
  store          SQLite schema + CRUD
  cli            Command-line entry point

See README.md for quickstart.
"""

__version__ = "0.0.1"
