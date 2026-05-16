"""Collect WARNING/ERROR/CRITICAL log lines from every observable
source and serve them to the `/issues` page.

Sources, in priority order:
  1. journald units `quantopsai` (scheduler) + `quantopsai-web` (gunicorn)
  2. altdata cron logs (`/opt/quantopsai/logs/altdata-*.log` and
     `edgar_form4_*.log`)
  3. Per-altdata scrape_runs rows where status != 'ok'

Grouping: identical messages within the same source are deduped into
one row with an `occurrences` count + `first_seen` / `last_seen`
timestamps. This prevents 1000+ "Option-premium fetch returned 0"
spam from drowning out a single ERROR no one's seen yet.

Read-only. Never raises — the page must render even if journald,
the filesystem, or any DB is unavailable. Each source failure shows
up as its own row in the output ("source X unavailable: ...") so
the failure of the issues collector itself is observable.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
from collections import defaultdict
from datetime import datetime
from glob import glob
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Default window for the page. Tunable via the route arg.
DEFAULT_WINDOW_HOURS = 24

# Patterns that match a level token in a log line.
_LEVEL_RE = re.compile(
    r"\[(WARNING|ERROR|CRITICAL)\]",
    re.IGNORECASE,
)

# Strip dynamic bits (UUIDs, timestamps, OCC symbols, hex IDs) so
# "Option-premium fetch returned 0 for X" and "...for Y" group together.
_DYN_PATTERNS = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                r"[0-9a-f]{4}-[0-9a-f]{12}\b"), "<uuid>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?\b"),
     "<ts>"),
    (re.compile(r"\b[A-Z]{1,6}\d{6}[CP]\d{8}\b"), "<occ>"),
    (re.compile(r"\bprofile[ _]?\d+\b", re.IGNORECASE), "profile <n>"),
    (re.compile(r"\bid=\d+\b"), "id=<n>"),
    (re.compile(r"\b#\d{1,9}\b"), "#<n>"),
    (re.compile(r"\b\d{4,}\b"), "<n>"),
]


def _signature(message: str) -> str:
    """Reduce a message to a comparison key for grouping."""
    sig = message
    for pat, repl in _DYN_PATTERNS:
        sig = pat.sub(repl, sig)
    return sig.strip()


# ---------------------------------------------------------------------------
# journald collector
# ---------------------------------------------------------------------------

def _collect_journald(
    units: List[str],
    since_hours: int,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Return (rows, error_message). error_message is None on success."""
    cmd = [
        "journalctl",
        "--no-pager",
        "--output=json",
        "--priority=warning",
        f"--since={since_hours} hours ago",
    ]
    for u in units:
        cmd.extend(["-u", u])

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15,
        )
    except FileNotFoundError:
        return [], (
            "journalctl not available on this host — journald collector "
            "cannot run (likely a non-Linux dev environment)"
        )
    except subprocess.TimeoutExpired:
        return [], "journalctl timed out after 15s"
    except OSError as exc:
        return [], f"journalctl OS error: {type(exc).__name__}: {exc}"

    if proc.returncode != 0:
        return [], (
            f"journalctl exit {proc.returncode}: "
            f"{(proc.stderr or '').strip()[:300]}"
        )

    rows: List[Dict[str, Any]] = []
    # systemd PRIORITY field: 3=err, 4=warning, 5=notice, 6=info, 7=debug
    # We requested --priority=warning so anything ≤ 4 surfaces.
    for line in (proc.stdout or "").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = obj.get("MESSAGE") or ""
        if not msg:
            continue
        prio_raw = obj.get("PRIORITY")
        try:
            prio = int(prio_raw) if prio_raw is not None else 6
        except (TypeError, ValueError):
            prio = 6
        # Map systemd priority → level label
        if prio <= 2:
            level = "CRITICAL"
        elif prio == 3:
            level = "ERROR"
        elif prio == 4:
            level = "WARNING"
        else:
            # Application-emitted higher-priority messages can still
            # carry [ERROR]/[WARNING] tags in the text (Python logger
            # writes to stderr → systemd assigns PRIORITY=6). Use the
            # text-tag fallback so those don't get dropped.
            tag = _LEVEL_RE.search(msg)
            if not tag:
                continue
            level = tag.group(1).upper()
        # Parse the realtime timestamp (microseconds since epoch).
        ts_us = obj.get("__REALTIME_TIMESTAMP")
        try:
            ts = datetime.fromtimestamp(int(ts_us) / 1_000_000)
            ts_iso = ts.isoformat(timespec="seconds")
        except (TypeError, ValueError):
            ts_iso = ""
        rows.append({
            "source": obj.get("_SYSTEMD_UNIT") or "journald",
            "level": level,
            "message": msg,
            "timestamp": ts_iso,
        })
    return rows, None


# ---------------------------------------------------------------------------
# altdata log files
# ---------------------------------------------------------------------------

def _altdata_log_paths(since_hours: int) -> List[str]:
    """Today's + yesterday's altdata + edgar_form4 logs on the droplet."""
    base = "/opt/quantopsai/logs"
    if not os.path.isdir(base):
        return []
    out = []
    for pattern in ("altdata-*.log", "edgar_form4_*.log",
                    "reconcile-cron.log"):
        out.extend(sorted(glob(os.path.join(base, pattern))))
    # Most recent 4 files of each — keeps scan bounded.
    return out[-12:]


def _collect_altdata_logs(
    since_hours: int,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Tail the most recent altdata logs for ERROR/WARNING lines."""
    paths = _altdata_log_paths(since_hours)
    if not paths:
        return [], None  # not an error — just no altdata logs here
    rows: List[Dict[str, Any]] = []
    errors: List[str] = []
    for p in paths:
        try:
            with open(p, "r", errors="replace") as f:
                # Bounded read — most recent 200KB of each file is
                # plenty for a 24h window.
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 200_000))
                for line in f:
                    m = _LEVEL_RE.search(line)
                    if not m:
                        continue
                    level = m.group(1).upper()
                    rows.append({
                        "source": os.path.basename(p),
                        "level": level,
                        "message": line.strip(),
                        "timestamp": "",
                    })
        except OSError as exc:
            errors.append(f"read {p}: {type(exc).__name__}: {exc}")
    err_str = "; ".join(errors) if errors else None
    return rows, err_str


# ---------------------------------------------------------------------------
# scrape_runs from each altdata DB
# ---------------------------------------------------------------------------

_ALTDATA_DBS = [
    ("congresstrades",
     "/opt/quantopsai/altdata/congresstrades/data/congresstrades.db"),
    ("edgar13f",
     "/opt/quantopsai/altdata/edgar13f/data/edgar13f.db"),
    ("edgar_form4",
     "/opt/quantopsai/altdata/edgar_form4/data/edgar_form4.db"),
    ("biotechevents",
     "/opt/quantopsai/altdata/biotechevents/data/biotechevents.db"),
    ("stocktwits",
     "/opt/quantopsai/altdata/stocktwits/data/stocktwits.db"),
]


def _collect_scrape_runs(
    since_hours: int,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Pull non-'ok' scrape_runs rows from each altdata DB."""
    rows: List[Dict[str, Any]] = []
    errors: List[str] = []
    for name, db_path in _ALTDATA_DBS:
        if not os.path.exists(db_path):
            continue
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                cur = conn.execute(
                    "SELECT source, status, started_at, finished_at, "
                    "error FROM scrape_runs "
                    "WHERE status NOT IN ('ok', 'running') "
                    "AND started_at >= datetime('now', "
                    "    '-' || ? || ' hours') "
                    "ORDER BY started_at DESC LIMIT 200",
                    (since_hours,),
                ).fetchall()
                for r in cur:
                    rows.append({
                        "source": f"{name}.scrape_runs",
                        "level": ("ERROR" if r["status"] == "failed"
                                  else "WARNING"),
                        "message": (
                            f"{r['source']}: status={r['status']}"
                            + (f" — {r['error']}" if r['error'] else "")
                        ),
                        "timestamp": r["started_at"] or "",
                    })
            finally:
                conn.close()
        except (sqlite3.OperationalError, sqlite3.DatabaseError,
                OSError) as exc:
            errors.append(
                f"{name}: {type(exc).__name__}: {exc}"
            )
    err_str = "; ".join(errors) if errors else None
    return rows, err_str


# ---------------------------------------------------------------------------
# Top-level aggregator
# ---------------------------------------------------------------------------

def collect_issues(
    since_hours: int = DEFAULT_WINDOW_HOURS,
    level_filter: Optional[str] = None,
) -> Dict[str, Any]:
    """Aggregate every WARN/ERROR/CRITICAL across all sources within
    the time window. Groups identical-signature messages within a
    source so spam doesn't drown out real issues.

    Returns dict shape:
        {
            "window_hours": int,
            "total_events": int,
            "total_groups": int,
            "groups": [
                {"source", "level", "signature", "sample_message",
                 "occurrences", "first_seen", "last_seen"},
                ...
            ],
            "source_errors": ["...", ...],  # collector itself
        }
    """
    journald_rows, j_err = _collect_journald(
        ["quantopsai", "quantopsai-web"], since_hours,
    )
    altdata_rows, a_err = _collect_altdata_logs(since_hours)
    scrape_rows, s_err = _collect_scrape_runs(since_hours)

    all_rows = journald_rows + altdata_rows + scrape_rows
    if level_filter:
        wanted = {x.upper() for x in level_filter.split(",")}
        all_rows = [r for r in all_rows if r["level"] in wanted]

    # Group by (source, level, signature)
    grouped: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for r in all_rows:
        sig = _signature(r["message"])
        key = (r["source"], r["level"], sig)
        g = grouped.get(key)
        if g is None:
            grouped[key] = {
                "source": r["source"],
                "level": r["level"],
                "signature": sig,
                "sample_message": r["message"],
                "occurrences": 1,
                "first_seen": r["timestamp"],
                "last_seen": r["timestamp"],
            }
        else:
            g["occurrences"] += 1
            # Keep first_seen as the EARLIEST, last_seen as the LATEST.
            if r["timestamp"]:
                if not g["first_seen"] or r["timestamp"] < g["first_seen"]:
                    g["first_seen"] = r["timestamp"]
                if not g["last_seen"] or r["timestamp"] > g["last_seen"]:
                    g["last_seen"] = r["timestamp"]

    # Sort: ERROR/CRITICAL first, then most-recent last_seen.
    level_rank = {"CRITICAL": 0, "ERROR": 1, "WARNING": 2}

    def _sort_key(g):
        return (
            level_rank.get(g["level"], 9),
            # newer last_seen first → invert
            -1 * _ts_int(g["last_seen"]),
        )

    groups = sorted(grouped.values(), key=_sort_key)

    return {
        "window_hours": since_hours,
        "total_events": sum(g["occurrences"] for g in groups),
        "total_groups": len(groups),
        "groups": groups,
        "source_errors": [
            e for e in [
                f"journald: {j_err}" if j_err else None,
                f"altdata logs: {a_err}" if a_err else None,
                f"scrape_runs: {s_err}" if s_err else None,
            ] if e
        ],
    }


def _ts_int(ts_iso: str) -> int:
    """Sort helper: convert ISO timestamp to int seconds (0 if blank)."""
    if not ts_iso:
        return 0
    try:
        return int(datetime.fromisoformat(ts_iso).timestamp())
    except (TypeError, ValueError):
        return 0


def issues_count(since_hours: int = DEFAULT_WINDOW_HOURS) -> Dict[str, int]:
    """Lightweight count for the nav badge."""
    summary = collect_issues(since_hours=since_hours)
    n_err = sum(g["occurrences"] for g in summary["groups"]
                if g["level"] in ("ERROR", "CRITICAL"))
    n_warn = sum(g["occurrences"] for g in summary["groups"]
                 if g["level"] == "WARNING")
    return {
        "errors": n_err,
        "warnings": n_warn,
        "total": n_err + n_warn,
    }
