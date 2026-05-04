"""ClinicalTrials.gov v2 API scraper.

API base: https://clinicaltrials.gov/api/v2/studies
Documentation: https://clinicaltrials.gov/data-api/api

Free, no API key. Their docs allow up to 50 req/sec but we use 1 req/sec
to be a polite citizen. Each request returns up to 1000 studies.

Strategy:
  - For ongoing scrape, query by `lastUpdatePostDate` desc — newest changes first
  - For backfill, query by phase + status filters
  - Each batch goes through raw_filings → normalize → upsert_trial

Change detection (Phase 2 → Phase 3 transition, status change to
SUSPENDED/TERMINATED) happens in store.upsert_trial — it compares
incoming values with the existing row and writes a trial_changes row
for every meaningful diff.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any, Dict, Iterable, List, Optional

import requests

from .normalize import (
    normalize_date,
    normalize_phase,
    normalize_status,
    sponsor_to_ticker,
)
from .store import (
    finish_run,
    insert_raw_filing,
    mark_raw_parsed,
    start_run,
    upsert_trial,
)

logger = logging.getLogger(__name__)


BASE = "https://clinicaltrials.gov/api/v2/studies"
USER_AGENT = "biotechevents Research Tool mack@mackenziesmith.com"

# Politeness: their docs allow much faster but we don't need real-time.
# 1 req/sec matches the broader ALTDATA_PLAN politeness target.
REQUEST_DELAY_SEC = 1.0

PARSER_VERSION = "ct-v2-json-v1"

# Page size — API caps at 1000, we use 200 for memory efficiency.
PAGE_SIZE = 200


class RateLimitedError(Exception):
    pass


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _get(url: str, params: Optional[Dict[str, Any]] = None) -> requests.Response:
    time.sleep(REQUEST_DELAY_SEC)
    r = requests.get(
        url, params=params, timeout=30,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    if r.status_code in (429, 403):
        raise RateLimitedError(
            f"ClinicalTrials.gov returned HTTP {r.status_code}. "
            f"Re-run later — cached rows preserved."
        )
    r.raise_for_status()
    return r


# ---------------------------------------------------------------------------
# Field extraction — pure function, easy to test
# ---------------------------------------------------------------------------

def parse_study(study: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the fields we care about from a single ClinicalTrials.gov
    study record (v2 API JSON shape).

    Returns a dict with the keys upsert_trial expects.
    """
    proto = study.get("protocolSection", {}) or {}

    ident = proto.get("identificationModule", {}) or {}
    nct_id = ident.get("nctId", "")
    brief_title = ident.get("briefTitle", "") or "(no title)"

    sponsor_mod = proto.get("sponsorCollaboratorsModule", {}) or {}
    lead = sponsor_mod.get("leadSponsor", {}) or {}
    sponsor_name = lead.get("name") or None
    sponsor_class = lead.get("class") or None

    status_mod = proto.get("statusModule", {}) or {}
    overall_status = normalize_status(status_mod.get("overallStatus"))
    primary_completion = (
        (status_mod.get("primaryCompletionDateStruct") or {}).get("date")
    )
    completion = (
        (status_mod.get("completionDateStruct") or {}).get("date")
    )
    start = (status_mod.get("startDateStruct") or {}).get("date")
    last_updated = status_mod.get("lastUpdatePostDateStruct", {}).get("date")

    design_mod = proto.get("designModule", {}) or {}
    phases = design_mod.get("phases") or []
    phase = normalize_phase("_".join(phases) if phases else None)

    enrollment = (design_mod.get("enrollmentInfo") or {}).get("count")

    conditions = (
        (proto.get("conditionsModule") or {}).get("conditions") or []
    )
    interventions = [
        i.get("name") for i in (
            (proto.get("armsInterventionsModule") or {}).get("interventions") or []
        ) if i.get("name")
    ]

    return {
        "nct_id": nct_id,
        "brief_title": brief_title[:300],
        "sponsor_name": sponsor_name,
        "sponsor_class": sponsor_class,
        "ticker": sponsor_to_ticker(sponsor_name),
        "phase": phase,
        "overall_status": overall_status,
        "primary_completion_date": normalize_date(primary_completion),
        "completion_date": normalize_date(completion),
        "start_date": normalize_date(start),
        "last_updated": normalize_date(last_updated),
        "enrollment_count": enrollment,
        "conditions": conditions,
        "interventions": interventions,
    }


# ---------------------------------------------------------------------------
# Top-level scrape
# ---------------------------------------------------------------------------

def fetch_recently_updated(
    db_conn: sqlite3.Connection,
    days_back: int = 7,
    max_pages: Optional[int] = None,
) -> Dict[str, int]:
    """Pull studies updated in the last N days.

    Pages through results. Each study goes through raw_filings → parse
    → upsert. The change-detection logic in upsert_trial writes
    trial_changes rows automatically.
    """
    run_id = start_run(db_conn, "clinicaltrials")
    stats = {"trials_seen": 0, "trials_new": 0, "trials_updated": 0,
             "changes_detected": 0, "errors": 0}

    try:
        from datetime import datetime, timedelta
        cutoff = (datetime.utcnow() - timedelta(days=days_back)).strftime("%Y-%m-%d")

        page_token: Optional[str] = None
        page_num = 0

        while True:
            page_num += 1
            params: Dict[str, Any] = {
                "filter.advanced": f"AREA[LastUpdatePostDate]RANGE[{cutoff},MAX]",
                "pageSize": PAGE_SIZE,
                "format": "json",
            }
            if page_token:
                params["pageToken"] = page_token

            r = _get(BASE, params=params)
            data = r.json()

            studies = data.get("studies", []) or []
            for raw in studies:
                stats["trials_seen"] += 1
                try:
                    parsed = parse_study(raw)
                    if not parsed["nct_id"]:
                        continue

                    # Persist raw before parsing-side effects
                    insert_raw_filing(
                        db_conn, source="clinicaltrials",
                        external_id=parsed["nct_id"],
                        content_type="json",
                        payload=json.dumps(raw, default=str),
                        source_url=f"https://clinicaltrials.gov/study/{parsed['nct_id']}",
                    )

                    result = upsert_trial(
                        db_conn,
                        parser_version=PARSER_VERSION,
                        **{k: v for k, v in parsed.items()
                           if k not in ("conditions", "interventions")},
                        conditions=parsed["conditions"],
                        interventions=parsed["interventions"],
                    )
                    if result["is_new"]:
                        stats["trials_new"] += 1
                    else:
                        stats["trials_updated"] += 1
                    stats["changes_detected"] += len(result["changes"])

                    mark_raw_parsed(db_conn, "clinicaltrials",
                                     parsed["nct_id"], "parsed")
                except Exception as exc:
                    stats["errors"] += 1
                    logger.debug("Trial parse failed: %s", exc)

            # Commit per page so a failure halfway through doesn't lose work
            db_conn.commit()
            logger.info(
                "  page %d: seen %d, new %d, updated %d, changes %d",
                page_num, stats["trials_seen"],
                stats["trials_new"], stats["trials_updated"],
                stats["changes_detected"],
            )

            page_token = data.get("nextPageToken")
            if not page_token:
                break
            if max_pages and page_num >= max_pages:
                break

        finish_run(db_conn, run_id, status="ok",
                   rows_inserted=stats["trials_new"] + stats["trials_updated"],
                   rows_seen=stats["trials_seen"])
    except RateLimitedError as exc:
        finish_run(db_conn, run_id, status="failed",
                   rows_inserted=stats["trials_new"] + stats["trials_updated"],
                   rows_seen=stats["trials_seen"],
                   error=f"rate limited: {exc}")
        raise
    except Exception as exc:
        logger.exception("ClinicalTrials scrape failed")
        finish_run(db_conn, run_id, status="failed",
                   rows_inserted=stats["trials_new"] + stats["trials_updated"],
                   rows_seen=stats["trials_seen"],
                   error=str(exc))
        raise

    return stats


def fetch_for_ticker(
    db_conn: sqlite3.Connection,
    sponsor_name: str,
    max_pages: int = 5,
) -> Dict[str, int]:
    """Pull all trials for a specific sponsor (regardless of update date).

    Useful for first-time backfill of a watchlist company's trials.
    """
    run_id = start_run(db_conn, f"clinicaltrials:{sponsor_name}")
    stats = {"trials_seen": 0, "trials_new": 0, "trials_updated": 0,
             "errors": 0}

    try:
        page_token: Optional[str] = None
        page_num = 0
        while True:
            page_num += 1
            params: Dict[str, Any] = {
                "query.spons": sponsor_name,
                "pageSize": PAGE_SIZE,
                "format": "json",
            }
            if page_token:
                params["pageToken"] = page_token

            r = _get(BASE, params=params)
            data = r.json()
            for raw in data.get("studies", []) or []:
                stats["trials_seen"] += 1
                parsed = parse_study(raw)
                if not parsed["nct_id"]:
                    continue
                insert_raw_filing(
                    db_conn, source="clinicaltrials",
                    external_id=parsed["nct_id"],
                    content_type="json",
                    payload=json.dumps(raw, default=str),
                    source_url=f"https://clinicaltrials.gov/study/{parsed['nct_id']}",
                )
                result = upsert_trial(
                    db_conn, parser_version=PARSER_VERSION,
                    **{k: v for k, v in parsed.items()
                       if k not in ("conditions", "interventions")},
                    conditions=parsed["conditions"],
                    interventions=parsed["interventions"],
                )
                if result["is_new"]:
                    stats["trials_new"] += 1
                else:
                    stats["trials_updated"] += 1
                mark_raw_parsed(db_conn, "clinicaltrials",
                                 parsed["nct_id"], "parsed")
            db_conn.commit()
            page_token = data.get("nextPageToken")
            if not page_token or page_num >= max_pages:
                break

        finish_run(db_conn, run_id, status="ok",
                   rows_inserted=stats["trials_new"] + stats["trials_updated"],
                   rows_seen=stats["trials_seen"])
    except Exception as exc:
        finish_run(db_conn, run_id, status="failed",
                   rows_inserted=stats["trials_new"] + stats["trials_updated"],
                   rows_seen=stats["trials_seen"],
                   error=str(exc))
        raise

    return stats
