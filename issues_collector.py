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
#
# The first two timestamp patterns are load-bearing for dedup: python's
# logger emits the local datetime + millisecond marker (e.g.
# "2026-06-04 20:02:35,726") at the start of every line journald
# captures, and ISO 8601 with a 'T' separator turns up in payloads.
# Without stripping both, every otherwise-identical log entry has a
# unique signature and the /issues page renders 611 rows instead of
# the ~20 distinct issues actually firing.
_DYN_PATTERNS = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                r"[0-9a-f]{4}-[0-9a-f]{12}\b"), "<uuid>"),
    # python logger format: 2026-06-04 20:02:35,726 (with or without
    # the comma-millisecond) — what journald sees on every line
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}"
                r"(?:[.,]\d+)?Z?\b"), "<ts>"),
    # Time-only suffix (HH:MM:SS[.ms])
    (re.compile(r"\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b"), "<time>"),
    (re.compile(r"\b[A-Z]{1,6}\d{6}[CP]\d{8}\b"), "<occ>"),
    (re.compile(r"\bprofile[ _]?\d+\b", re.IGNORECASE), "profile <n>"),
    # quantopsai_profile_<n>.db filename references
    (re.compile(r"quantopsai_profile_\d+\.db"),
     "quantopsai_profile_<n>.db"),
    # account references like "Account 14" / "account_id=14"
    (re.compile(r"\b(?:Account|account_id=)\s*\d+\b"),
     "Account <n>"),
    # bare profile name prefixes (EXP-A1- / EXP-A2- / etc.) — keep
    # the experiment-arm prefix but strip the trailing pid in brackets
    (re.compile(r"\[EXP-[AB][0-9]-[A-Za-z0-9_-]+\]"),
     "[EXP-<arm>]"),
    (re.compile(r"\bid=\d+\b"), "id=<n>"),
    (re.compile(r"\b#\d{1,9}\b"), "#<n>"),
    (re.compile(r"\bqty=[\d.]+\b"), "qty=<n>"),
    (re.compile(r"\bmedian=[\d.]+\b"), "median=<n>"),
    (re.compile(r"\b\d+\.\d+x\b"), "<n>x"),  # "5.8x median"
    # percentages and ratios
    (re.compile(r"\b\d+/\d+\b"), "<n>/<n>"),
    (re.compile(r"\(\d+\.\d+%\)"), "(<n>%)"),
    # naked floor message: "floor 80.0%"
    (re.compile(r"floor \d+\.\d+%"), "floor <n>%"),
    (re.compile(r"\b\d{4,}\b"), "<n>"),
    # Trailing list payloads — the prefix is what tells us "stop
    # coverage breach"; the specific list of naked symbols / rejected
    # tickers etc. just varies on every cycle and should be collapsed.
    (re.compile(r"Naked: .+$"), "Naked: <symbols>"),
    (re.compile(r"failed symbols?: .+$", re.IGNORECASE),
     "failed symbols: <list>"),
    # Bare single-digit counts (e.g. "skipped 2 row(s)" vs
    # "skipped 1 row(s)") — different counts of the same underlying
    # issue should still group
    (re.compile(r"\bskipped \d+ row"), "skipped <n> row"),
    (re.compile(r"\(\d+ row(?:s?)\)"), "(<n> rows)"),
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


# Timestamp prefix patterns seen in altdata logs:
#   1. Python logging default: "2026-05-16 06:08:37,844 [LEVEL] ..."
#   2. ISO + Z: "2026-05-16T06:08:37Z [LEVEL] ..."
#   3. Rich console wraps: any leading "[2026-05-16 06:08:37]"
# Extract the first timestamp on the line so /issues can show when
# the event actually happened rather than when the page was rendered.
_LOG_TS_RE = re.compile(
    r"\b(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})"
)


def _extract_log_timestamp(line: str) -> str:
    """Return ISO timestamp 'YYYY-MM-DDTHH:MM:SS' if the line begins
    with (or contains within first ~50 chars) a standard timestamp.
    Returns '' when nothing parseable is found — caller renders '—'.
    """
    head = line[:80]
    m = _LOG_TS_RE.search(head)
    if not m:
        return ""
    return f"{m.group(1)}T{m.group(2)}"


def _collect_altdata_logs(
    since_hours: int,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Tail the most recent altdata logs for ERROR/WARNING lines.

    Now (2026-05-16) extracts the per-line timestamp so /issues shows
    when each event actually happened. Pre-fix every altdata-derived
    row had an empty timestamp, making them indistinguishable from
    just-fired events — user couldn't tell if an error was ancient
    residue or live.
    """
    paths = _altdata_log_paths(since_hours)
    if not paths:
        return [], None  # not an error — just no altdata logs here
    from datetime import datetime, timedelta
    cutoff_dt = datetime.utcnow() - timedelta(hours=since_hours)
    cutoff_iso = cutoff_dt.isoformat(timespec="seconds")
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
                    ts = _extract_log_timestamp(line)
                    # Time-window filter: events older than the
                    # requested window are stale residue. Pre-fix
                    # we returned everything in the file (up to 7d
                    # of historical altdata cron logs), making the
                    # /issues page show errors that had already
                    # been fixed but the old log lines hadn't aged
                    # out.
                    if ts and ts < cutoff_iso:
                        continue
                    level = m.group(1).upper()
                    rows.append({
                        "source": os.path.basename(p),
                        "level": level,
                        "message": line.strip(),
                        "timestamp": ts,
                    })
        except OSError as exc:
            errors.append(f"read {p}: {type(exc).__name__}: {exc}")
    err_str = "; ".join(errors) if errors else None
    return rows, err_str


# ---------------------------------------------------------------------------
# Aggregate journal-vs-broker drift (live)
# ---------------------------------------------------------------------------

# 1h in-process cache so the /issues page isn't slow + doesn't
# hammer Alpaca's list_positions on every reload.
_DRIFT_CACHE: Dict[str, Any] = {"ts": 0.0, "rows": [], "error": None}
_DRIFT_CACHE_TTL_SEC = 3600


def _collect_aggregate_drift(
    since_hours: int,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Live snapshot of aggregate journal-vs-broker drift.

    Pre-2026-05-16 the only way to surface drift on /issues was the
    journald ERROR emitted by the gated profile-1 reconcile task —
    which only fires during market-hours scan cycles. On weekends
    the 123 outstanding drift items were invisible. This adds a
    live check so drift is surfaced whenever /issues is rendered,
    1h cached to keep the page fast and within Alpaca rate limits.
    """
    import time as _time
    now = _time.time()
    if (now - _DRIFT_CACHE["ts"] < _DRIFT_CACHE_TTL_SEC
            and _DRIFT_CACHE["rows"] is not None):
        return _DRIFT_CACHE["rows"], _DRIFT_CACHE["error"]

    rows: List[Dict[str, Any]] = []
    err: Optional[str] = None
    try:
        from aggregate_audit import audit_aggregate_drift
        audit = audit_aggregate_drift(profile_ids=range(1, 12))
        for d in audit.get("drift", []):
            # aggregate_audit returns `account` + `kind`; tolerate
            # alternate key names from older callers + tests.
            acct = (d.get("account") or d.get("alpaca_account_id")
                    or d.get("acct") or "?")
            sym = d.get("symbol", "?")
            j = d.get("journal_qty", 0)
            b = d.get("broker_qty", 0)
            delta = d.get("drift", 0)
            cat = d.get("kind") or d.get("category") or "drift"
            level = "ERROR"
            rows.append({
                "source": f"aggregate_audit.{acct}",
                "level": level,
                "message": (
                    f"{cat}: {sym} journal={j:+.2f} broker={b:+.2f} "
                    f"drift={delta:+.2f}"
                ),
                # Drift is a LIVE snapshot, not a point-in-time
                # event. The underlying state may have existed for
                # days. Marked empty so the UI renders "—" with a
                # "live snapshot" badge — pre-2026-05-16 this used
                # `datetime.utcnow()` which made days-old residue
                # look like a fresh event on every page load.
                "timestamp": "",
                "is_live_snapshot": True,
            })
    except ImportError as exc:
        err = f"aggregate_audit unavailable: {exc}"
    except Exception as exc:
        err = f"aggregate_audit raised: {type(exc).__name__}: {exc}"

    # Value-parity audit (#165, 2026-05-17). Quantity audit catches
    # share-count drift; this catches dollar drift even when shares
    # match (different marks, missing multipliers, etc.).
    try:
        from aggregate_audit import audit_account_value_parity
        v_audit = audit_account_value_parity(profile_ids=range(1, 12))
        for d in v_audit.get("drift", []):
            acct = d.get("account", "?")
            cat = d.get("kind") or "value_drift"
            rows.append({
                "source": f"value_parity.{acct}",
                "level": "ERROR",
                "message": (
                    f"{cat}: broker=${d.get('broker_value', 0):,.2f} "
                    f"journal=${d.get('journal_value', 0):,.2f} "
                    f"drift=${d.get('drift', 0):+,.2f} "
                    f"(tol=${d.get('tolerance', 0):,.2f}, profiles="
                    f"{d.get('profile_ids', [])})"
                ),
                "timestamp": "",
                "is_live_snapshot": True,
            })
    except ImportError as exc:
        # If qty audit was loaded but value audit isn't yet, surface
        # the install issue rather than silently skipping the check.
        if not err:
            err = f"value_parity audit unavailable: {exc}"
        else:
            err += f" | value_parity audit unavailable: {exc}"
    except Exception as exc:
        ve = f"value_parity audit raised: {type(exc).__name__}: {exc}"
        err = ve if not err else (err + " | " + ve)

    # Equity-identity audit (#166, 2026-05-17). The journal's own algebra
    # must balance: equity == initial_capital + realized + unrealized.
    # Catches FIFO mismatch, hidden cash flows, market_value vs
    # unrealized_pl divergence — bugs the other two audits can't see.
    try:
        from integrity_audit import audit_equity_identity_all
        i_audit = audit_equity_identity_all(profile_ids=range(1, 12))
        for d in i_audit.get("drift", []):
            pid = d.get("profile_id", "?")
            rows.append({
                "source": f"equity_identity.profile_{pid}",
                "level": "ERROR",
                "message": (
                    f"equity identity broken: expected=${d.get('expected_equity', 0):,.2f} "
                    f"actual=${d.get('actual_equity', 0):,.2f} "
                    f"drift=${d.get('drift', 0):+,.2f} "
                    f"(init=${d.get('initial_capital', 0):,.2f}, "
                    f"realized=${d.get('realized_total', 0):+,.2f}, "
                    f"unrealized=${d.get('unrealized_total', 0):+,.2f})"
                ),
                "timestamp": "",
                "is_live_snapshot": True,
            })
    except ImportError as exc:
        ie = f"equity_identity audit unavailable: {exc}"
        err = ie if not err else (err + " | " + ie)
    except Exception as exc:
        ie = f"equity_identity audit raised: {type(exc).__name__}: {exc}"
        err = ie if not err else (err + " | " + ie)

    # Cash-parity audit (#167, 2026-05-17). Per Alpaca account:
    # broker cash should equal sum of virtual cash across profiles
    # routing to it. Catches hidden broker cash flow (dividend, fee,
    # manual deposit) and trades that hit broker but not the journal.
    try:
        from aggregate_audit import audit_account_cash_parity
        c_audit = audit_account_cash_parity(profile_ids=range(1, 12))
        for d in c_audit.get("drift", []):
            acct = d.get("account", "?")
            rows.append({
                "source": f"cash_parity.{acct}",
                "level": "ERROR",
                "message": (
                    f"{d.get('kind', 'cash_drift')}: "
                    f"broker_cash=${d.get('broker_cash', 0):,.2f} "
                    f"journal_cash=${d.get('journal_cash', 0):,.2f} "
                    f"drift=${d.get('drift', 0):+,.2f} "
                    f"(tol=${d.get('tolerance', 0):,.2f}, profiles="
                    f"{d.get('profile_ids', [])})"
                ),
                "timestamp": "",
                "is_live_snapshot": True,
            })
    except ImportError as exc:
        ce = f"cash_parity audit unavailable: {exc}"
        err = ce if not err else (err + " | " + ce)
    except Exception as exc:
        ce = f"cash_parity audit raised: {type(exc).__name__}: {exc}"
        err = ce if not err else (err + " | " + ce)

    # Reconciler heartbeat (#170, 2026-05-17). All audits are useless
    # if the reconciler isn't running. Stale (>60min) per profile = error.
    try:
        from integrity_audit import audit_reconciler_heartbeat_all
        hb_audit = audit_reconciler_heartbeat_all(profile_ids=range(1, 12))
        for d in hb_audit.get("drift", []):
            pid = d.get("profile_id", "?")
            age = d.get("age_minutes")
            age_str = f"{age:.0f} min" if age is not None else "never"
            rows.append({
                "source": f"reconciler_heartbeat.profile_{pid}",
                "level": "ERROR",
                "message": (
                    f"reconciler stale for profile {pid}: last run "
                    f"{age_str} ago "
                    f"(threshold={d.get('max_age_minutes')} min). "
                    "All integrity audits are reading stale state — "
                    "check the scheduler / cron / host."
                ),
                "timestamp": "",
                "is_live_snapshot": True,
            })
    except ImportError as exc:
        he = f"reconciler_heartbeat audit unavailable: {exc}"
        err = he if not err else (err + " | " + he)
    except Exception as exc:
        he = f"reconciler_heartbeat raised: {type(exc).__name__}: {exc}"
        err = he if not err else (err + " | " + he)

    # Basis-parity audit (#167, 2026-05-17). Per (account, symbol):
    # broker avg_entry_price should match the qty-weighted virtual
    # avg_entry across all profiles holding that symbol. Catches
    # wrong-price fills, broken FIFO basis adjustment, multileg
    # cost-allocation drift.
    try:
        from aggregate_audit import audit_account_basis_parity
        b_audit = audit_account_basis_parity(profile_ids=range(1, 12))
        for d in b_audit.get("drift", []):
            acct = d.get("account", "?")
            sym = d.get("symbol", "?")
            rows.append({
                "source": f"basis_parity.{acct}",
                "level": "ERROR",
                "message": (
                    f"{d.get('kind', 'basis_drift')} {sym}: "
                    f"broker_avg=${d.get('broker_avg', 0):.4f} "
                    f"journal_avg=${d.get('journal_avg', 0):.4f} "
                    f"drift=${d.get('drift', 0):+.4f} "
                    f"(broker_qty={d.get('broker_qty', 0):.2f}, "
                    f"journal_qty={d.get('journal_qty', 0):.2f}, "
                    f"tol=${d.get('tolerance', 0):.4f}, profiles="
                    f"{d.get('profile_ids', [])})"
                ),
                "timestamp": "",
                "is_live_snapshot": True,
            })
    except ImportError as exc:
        be = f"basis_parity audit unavailable: {exc}"
        err = be if not err else (err + " | " + be)
    except Exception as exc:
        be = f"basis_parity audit raised: {type(exc).__name__}: {exc}"
        err = be if not err else (err + " | " + be)

    _DRIFT_CACHE["ts"] = now
    _DRIFT_CACHE["rows"] = rows
    _DRIFT_CACHE["error"] = err
    return rows, err


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
                    level = ("ERROR" if r["status"] == "failed"
                             else "WARNING")
                    base_ts = r["started_at"] or ""
                    err_raw = r["error"]
                    # Backward-compatible: try JSON decode for the
                    # per-item shape added 2026-05-16. Falls back to
                    # plain text for old runs / scrapers that haven't
                    # adopted the JSON format yet.
                    items = None
                    summary = err_raw
                    if err_raw and err_raw.startswith("{"):
                        try:
                            obj = json.loads(err_raw)
                            summary = obj.get("summary", err_raw)
                            items = obj.get("items")
                        except (json.JSONDecodeError, TypeError):
                            pass
                    rows.append({
                        "source": f"{name}.scrape_runs",
                        "level": level,
                        "message": (
                            f"{r['source']}: status={r['status']}"
                            + (f" — {summary}" if summary else "")
                        ),
                        "timestamp": base_ts,
                    })
                    # If JSON per-item detail is present, surface each
                    # failed item as its own row so the /issues page
                    # shows EXACTLY which ticker failed and why. Cap
                    # at 50 items per run to bound output for huge
                    # error lists.
                    if items:
                        for item in items[:50]:
                            label = item.get("label", "?")
                            reason = item.get("reason", "?")
                            rows.append({
                                "source": f"{name}.scrape_runs/{label}",
                                "level": level,
                                "message": (
                                    f"{name} {r['source']}: "
                                    f"{label} — {reason}"
                                ),
                                "timestamp": base_ts,
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
    drift_rows, d_err = _collect_aggregate_drift(since_hours)

    all_rows = journald_rows + altdata_rows + scrape_rows + drift_rows
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
                # Carry the live-snapshot flag through grouping so
                # the template can render "—" + "live snapshot"
                # badge instead of pretending the row has a moment-
                # in-time timestamp.
                "is_live_snapshot": r.get("is_live_snapshot", False),
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
                f"aggregate_audit: {d_err}" if d_err else None,
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
