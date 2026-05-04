"""biotechevents — clinical-trial milestones + FDA event calendar.

Local-first scraper for biotech catalysts:
  - ClinicalTrials.gov v2 API: every registered trial's phase, status,
    completion dates, sponsor, and study conditions
  - FDA approval calendar (PDUFA dates) — currently a stub; the FDA's
    public data flow is fragmented across multiple unstructured pages
    so v1 ships with ClinicalTrials only

Public modules:
  store               SQLite schema + CRUD + raw_filings layer
  scrape_clinicaltrials  ClinicalTrials.gov v2 JSON API
  scrape_fda          FDA PDUFA calendar (stub for v1)
  normalize           Sponsor name → ticker mapping
  cli                 click-based: daily, refresh, show, counts, runs

See README.md for quickstart. Plan context in
~/Quantops/ALTDATA_PLAN.md.
"""

__version__ = "0.0.1"
