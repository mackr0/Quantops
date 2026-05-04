"""FDA PDUFA event scraper — STUB.

Why this is hard and v1 ships without it:

1. FDA does NOT publish a single, structured, free PDUFA calendar.
   The official calendar is fragmented across:
     - FDA AdCom meeting schedule (HTML, no API)
     - Drug approvals page (HTML, periodic publication)
     - Press releases (RSS, but PDUFA dates rarely mentioned)
     - Drugs@FDA database (HTML lookups, not bulk-queryable)

2. Third-party scrapers have largely been killed by aggressive site
   changes:
     - BioPharmCatalyst publishes a calendar, but their TOS prohibits
       automated scraping
     - FierceBiotech has an RSS that mentions some PDUFA dates but
       is not comprehensive
     - Several open-source PDUFA scrapers have died in the last 2 years

3. Reliable alternatives, all paid:
     - BioPharmCatalyst Pro ($300+/yr)
     - PharmaIntelligence ($K/yr)
     - Cortellis ($$$$$)

Plan: ship v1 with ClinicalTrials.gov only (already very valuable —
phase transitions and primary completion dates ARE the leading
indicators for PDUFA decisions ~6-12 months later). Revisit FDA
direct scraping after observing what signal we actually need.

For when we're ready to tackle this:
  - Start with the FDA AdCom calendar HTML (most-stable URL)
  - Cross-reference adcom drug names + sponsor names with our
    `sponsor_to_ticker` map
  - Use SEC 8-K filings (already in our edgar13f raw data) to detect
    actual approval/rejection announcements (companies file 8-K
    within 24 hours of any material FDA decision)
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Dict

from .store import finish_run, start_run

logger = logging.getLogger(__name__)


def scrape_pdufa_calendar(db_conn: sqlite3.Connection) -> Dict[str, int]:
    """STUB: not yet implemented. Returns immediately so the daily
    pipeline doesn't fail."""
    run_id = start_run(db_conn, "fda")
    logger.warning(
        "FDA scraper not implemented — see scrape_fda.py docstring for why."
    )
    finish_run(
        db_conn, run_id, status="ok",
        rows_inserted=0, rows_seen=0,
        error="STUB: FDA PDUFA scraper not yet implemented",
    )
    return {"events_seen": 0, "events_inserted": 0, "note": "stub"}
