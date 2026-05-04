"""stocktwits — local cache of StockTwits messages and per-ticker sentiment.

Polls the StockTwits public REST API (free, no key required for the
endpoints we use) and stores messages + daily sentiment aggregates in
a local SQLite. Designed for a watchlist of ~50-200 tickers we care
about — not a firehose of everything.

Public modules:
  store          SQLite schema + CRUD
  scrape         StockTwits REST API client
  aggregate      Daily sentiment rollups + change detection
  cli            click-based: daily, refresh, show, trending, sentiment, runs

See README.md for quickstart. Plan context in
~/Quantops/ALTDATA_PLAN.md.
"""

__version__ = "0.0.1"
