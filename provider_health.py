"""AI provider health — the warning system (2026-11-02, docs/25 D8).

Why this exists. On 2026-11-02 the operator found from a P&L review
that nine of the twelve Experiment 2 books had made no AI decision
for two to six weeks: OpenAI retired `gpt-4.1-nano` on 10-23 (every
call 404 `model_not_found`), Google's prepaid credits ran out (402
`RESOURCE_EXHAUSTED`), and both Gemini arm models had been deprecated
on 10-08. The stops and sweeps kept running, so the books looked
alive; the only health probe covered Alpaca and yfinance; the model
picker's "available" badge read the vendor's list-models endpoint,
which still returned the retired id. This module is the check that
would have fired on 09-25.

Four checks, each with its own alert class, all fail-loud:

1. **Provider probe** (`probe_profile`, every PROBE_INTERVAL_SEC per
   active profile): a tiny call to the profile's own model with its
   own key. A failure is classified — DEPRECATED_MODEL / CREDITS /
   QUOTA / AUTH / OUTAGE / UNKNOWN — and persisted in the master DB
   (`provider_health`), so the alert state survives restarts.
2. **Deprecation lookahead** (`deprecation_lookahead`, daily): every
   (provider, model) in use is checked against the static lifecycle
   table `ai_providers.MODEL_STATUS` (vendor deprecation pages) and,
   for OpenAI, the live `shutdown_date` on GET /v1/models. Warns at
   60 / 30 / 7 days and on the day.
3. **Decision-flow watchdog** (`decision_flow_check`, per profile):
   the profile's last successful apex call (`ai_cost_ledger`,
   purpose `batch_select`) is older than STALL_MARKET_MINUTES of market
   time while the market was open → alert. This is independent of
   WHY — a broken prompt, a dead key, a silent exception all look the
   same from here, which is the point.
4. **Credit runway** (`credit_runway`, daily): trailing-7-day fleet
   spend per provider projected to a calendar month against
   `config.FLEET_MONTHLY_AI_BUDGET_USD`; warns above 80% and when any
   provider's probe says CREDITS.

Alerting: `activity_log` row + email (`notifications.notify_error`)
on the FIRST occurrence of a (profile, class), a daily repeat while
unresolved, and a recovery row when the probe passes again. The
email debounce in `notify_error` is per subject, so each class has
its own subject.

Fail-closed rule: a probe that cannot run (no key, no model, import
error) is a failure (`UNKNOWN`), never a pass.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

PROBE_INTERVAL_SEC = 30 * 60
PROBE_MAX_TOKENS = 32
PROBE_PROMPT = "Reply with the single word OK."
#: Market minutes without a successful apex call before the watchdog
#: fires. Cycles run every ~10 minutes; six hours of market time with
#: no decision is a dead book, not a quiet one.
STALL_MARKET_MINUTES = 360
LOOKAHEAD_DAYS = (60, 30, 7, 0)
RUNWAY_WARN_SHARE = 0.80
REPEAT_ALERT_HOURS = 24

# Failure classes, most specific first. Each: (class, status codes,
# message regex). Status is read from the exception when the SDK
# exposes it (`status_code` on openai/anthropic APIStatusError, `code`
# on google.genai ClientError); the regex covers wrapped strings.
DEPRECATED_MODEL = "DEPRECATED_MODEL"
CREDITS = "CREDITS"
QUOTA = "QUOTA"
AUTH = "AUTH"
OUTAGE = "OUTAGE"
UNKNOWN = "UNKNOWN"

_CLASS_RULES: Tuple[Tuple[str, Tuple[int, ...], str], ...] = (
    (DEPRECATED_MODEL, (404,),
     r"model_not_found|has been deprecated|deprecated|not found|does not exist|"
     r"unknown model|is not supported|retired|no longer available"),
    (CREDITS, (402,),
     r"credits|prepayment|billing|insufficient_quota|insufficient funds|"
     r"balance|payment required|spending cap|exceeded your current quota"),
    (QUOTA, (429,),
     r"rate limit|rate_limit|too many requests|resource_exhausted|quota"),
    (AUTH, (401, 403),
     r"api key|api_key|authentication|unauthorized|permission denied|"
     r"invalid x-api-key|forbidden"),
    (OUTAGE, (500, 502, 503, 504, 529),
     r"overloaded|service unavailable|internal server error|timeout|"
     r"timed out|connection|bad gateway"),
)

CLASS_EXPLANATIONS = {
    DEPRECATED_MODEL: ("The vendor no longer serves this model id. Pick a "
                       "current model in Settings → AI model (the catalog "
                       "marks replacements); until then this book makes "
                       "NO decisions."),
    CREDITS: ("The vendor account has no prepaid credit / billing has "
              "failed. Top up the vendor account; until then this book "
              "makes NO decisions."),
    QUOTA: ("Rate or spend quota exhausted at the vendor. Usually clears "
            "on its own; if it persists for hours the quota tier is too "
            "small for the fleet."),
    AUTH: ("The API key was rejected. It was revoked, rotated or belongs "
           "to the wrong provider. Re-enter it in Settings."),
    OUTAGE: ("The vendor is down or overloaded. The circuit breaker and "
             "fallback chain handle this; it is reported so a long outage "
             "is visible."),
    UNKNOWN: ("Unclassified failure — read the detail. Treated as a "
              "failure, never as a pass."),
}


# ---------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------

def _status_of(exc: BaseException) -> Optional[int]:
    for attr in ("status_code", "code", "status"):
        v = getattr(exc, attr, None)
        if isinstance(v, int) and 100 <= v <= 599:
            return v
    resp = getattr(exc, "response", None)
    v = getattr(resp, "status_code", None)
    if isinstance(v, int):
        return v
    m = re.search(r"\b(40[1-4]|429|5\d\d)\b", str(exc))
    return int(m.group(1)) if m else None


def classify_failure(exc: BaseException) -> Tuple[str, str]:
    """(class, one-line detail) for a provider exception."""
    msg = str(exc)
    low = msg.lower()
    status = _status_of(exc)
    # Message first for the two classes a vendor reports under an
    # ambiguous status (Google reports credit depletion as 402 but
    # OpenAI reports it as 429 insufficient_quota; OpenAI reports a
    # retired model as 404 with "deprecated" in the text).
    for cls, codes, rx in _CLASS_RULES:
        if cls in (DEPRECATED_MODEL, CREDITS) and re.search(rx, low):
            return cls, _one_line(msg)
    for cls, codes, rx in _CLASS_RULES:
        if status in codes:
            return cls, _one_line(msg)
    for cls, codes, rx in _CLASS_RULES:
        if re.search(rx, low):
            return cls, _one_line(msg)
    return UNKNOWN, _one_line(f"{type(exc).__name__}: {msg}")


def _one_line(s: str, n: int = 300) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n] + "…"


# ---------------------------------------------------------------------
# Persistence (master DB)
# ---------------------------------------------------------------------

def _conn():
    from models import _get_conn
    return _get_conn()


def ensure_schema(conn=None) -> None:
    sql = """
    CREATE TABLE IF NOT EXISTS provider_health (
        profile_id INTEGER NOT NULL,
        check_kind TEXT NOT NULL,            -- probe | stall | deprecation | runway
        provider TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        ok INTEGER NOT NULL,
        failure_class TEXT,
        detail TEXT NOT NULL DEFAULT '',
        checked_at TEXT NOT NULL,
        first_failed_at TEXT,
        last_alerted_at TEXT,
        PRIMARY KEY (profile_id, check_kind)
    )"""
    if conn is not None:
        conn.execute(sql)
        return
    with closing(_conn()) as c:
        c.execute(sql)
        c.commit()


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _read(profile_id: int, kind: str) -> Optional[Dict[str, Any]]:
    with closing(_conn()) as c:
        c.row_factory = sqlite3.Row
        ensure_schema(c)
        r = c.execute("SELECT * FROM provider_health WHERE profile_id=? AND check_kind=?",
                      (profile_id, kind)).fetchone()
        return dict(r) if r else None


def _write(row: Dict[str, Any]) -> None:
    with closing(_conn()) as c:
        ensure_schema(c)
        c.execute("""INSERT INTO provider_health
            (profile_id, check_kind, provider, model, ok, failure_class, detail,
             checked_at, first_failed_at, last_alerted_at)
            VALUES (:profile_id, :check_kind, :provider, :model, :ok, :failure_class,
                    :detail, :checked_at, :first_failed_at, :last_alerted_at)
            ON CONFLICT(profile_id, check_kind) DO UPDATE SET
              provider=excluded.provider, model=excluded.model, ok=excluded.ok,
              failure_class=excluded.failure_class, detail=excluded.detail,
              checked_at=excluded.checked_at, first_failed_at=excluded.first_failed_at,
              last_alerted_at=excluded.last_alerted_at""", row)
        c.commit()


def current_status() -> List[Dict[str, Any]]:
    """Every row, for the dashboard banner and /api/provider-health."""
    try:
        with closing(_conn()) as c:
            c.row_factory = sqlite3.Row
            ensure_schema(c)
            rows = c.execute("""SELECT h.*, p.name AS profile_name
                FROM provider_health h LEFT JOIN trading_profiles p ON p.id = h.profile_id
                ORDER BY h.ok, h.profile_id, h.check_kind""").fetchall()
            return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning("provider_health.current_status failed: %s", exc)
        return []


def open_failures() -> List[Dict[str, Any]]:
    return [r for r in current_status() if not r["ok"]]


# ---------------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------------

def _record_transition(profile_id: int, kind: str, provider: str, model: str,
                       ok: bool, failure_class: Optional[str], detail: str,
                       user_id: int = 1, profile_name: str = "") -> Dict[str, Any]:
    """Persist the check result and alert on the edges: first failure,
    daily repeat while failing, recovery. Returns the stored row."""
    prev = _read(profile_id, kind)
    now = _now_iso()
    row = {"profile_id": profile_id, "check_kind": kind, "provider": provider or "",
           "model": model or "", "ok": 1 if ok else 0, "failure_class": failure_class,
           "detail": detail or "", "checked_at": now,
           "first_failed_at": None, "last_alerted_at": None}
    label = profile_name or f"profile {profile_id}"
    if ok:
        if prev and not prev["ok"]:
            _activity(profile_id, user_id, "provider_health_recovered",
                      f"{label}: {kind} recovered ({provider}/{model})",
                      f"Previous failure {prev.get('failure_class')}: {prev.get('detail')}")
            logger.warning("PROVIDER HEALTH RECOVERED %s %s %s/%s", label, kind, provider, model)
        _write(row)
        return row
    first = (prev or {}).get("first_failed_at") if prev and not prev["ok"] else None
    row["first_failed_at"] = first or now
    last_alert = (prev or {}).get("last_alerted_at") if prev and not prev["ok"] else None
    due = (last_alert is None or
           datetime.fromisoformat(last_alert) <= datetime.now(timezone.utc) - timedelta(hours=REPEAT_ALERT_HOURS)
           or (prev or {}).get("failure_class") != failure_class)
    if due:
        title = f"{label}: AI {kind} FAILED — {failure_class} ({provider}/{model})"
        body = (f"{detail}\n\n{CLASS_EXPLANATIONS.get(failure_class or UNKNOWN, '')}\n\n"
                f"First seen: {row['first_failed_at']}. This alert repeats every "
                f"{REPEAT_ALERT_HOURS}h until the check passes.")
        _activity(profile_id, user_id, "provider_health_failed", title, body)
        logger.error("PROVIDER HEALTH FAILED %s", title)
        try:
            from notifications import notify_error
            notify_error(body, context=f"AI provider {failure_class} — {label}")
        except Exception as exc:
            logger.warning("provider_health email failed: %s", exc)
        row["last_alerted_at"] = now
    else:
        row["last_alerted_at"] = last_alert
    _write(row)
    return row


def _activity(profile_id: int, user_id: int, kind: str, title: str, detail: str) -> None:
    try:
        from models import log_activity
        log_activity(profile_id, user_id, kind, title, detail)
    except Exception as exc:
        logger.warning("provider_health activity_log failed: %s", exc)


# ---------------------------------------------------------------------
# 1. Provider probe
# ---------------------------------------------------------------------

def probe_model(provider: str, model: str, api_key: str) -> Tuple[bool, Optional[str], str, float]:
    """One tiny call. Returns (ok, failure_class, detail, latency_ms).
    Never raises."""
    t0 = time.time()
    if not provider or not model:
        return False, UNKNOWN, "profile has no provider/model configured", 0.0
    if not api_key:
        return False, AUTH, "profile has no API key for its provider", 0.0
    try:
        from ai_providers import _call_provider
        text, *_ = _call_provider(provider, PROBE_PROMPT, model, api_key, PROBE_MAX_TOKENS)
        ms = (time.time() - t0) * 1000
        if text is None:
            return False, UNKNOWN, "empty response from provider", ms
        return True, None, f"ok in {ms:.0f}ms", ms
    except Exception as exc:
        cls, detail = classify_failure(exc)
        return False, cls, detail, (time.time() - t0) * 1000


def probe_profile(ctx) -> Dict[str, Any]:
    provider = getattr(ctx, "ai_provider", "") or ""
    model = getattr(ctx, "ai_model", "") or ""
    key = getattr(ctx, "ai_api_key", "") or ""
    ok, cls, detail, _ms = probe_model(provider, model, key)
    return _record_transition(getattr(ctx, "profile_id", 0), "probe", provider, model,
                              ok, cls, detail, user_id=getattr(ctx, "user_id", 1),
                              profile_name=getattr(ctx, "display_name", "") or "")


# ---------------------------------------------------------------------
# 2. Deprecation lookahead
# ---------------------------------------------------------------------

def _openai_shutdown_date(model: str, api_key: str) -> Optional[str]:
    """OpenAI publishes `shutdown_date` (YYYY-MM-DD or null) on
    GET /v1/models/{id}. None when absent or unreachable."""
    try:
        from openai import OpenAI
        m = OpenAI(api_key=api_key).models.retrieve(model)
        d = getattr(m, "shutdown_date", None)
        if d is None and hasattr(m, "model_extra"):
            d = (m.model_extra or {}).get("shutdown_date")
        return str(d) if d else None
    except Exception as exc:
        logger.debug("shutdown_date lookup failed for %s: %s", model, exc)
        return None


def lifecycle_for(provider: str, model: str, api_key: str = "") -> Dict[str, Any]:
    """{status, retire_on, replacement, source} from the static table,
    overridden by a live vendor signal when one exists."""
    from ai_providers import MODEL_STATUS
    st = dict(MODEL_STATUS.get(model) or {"status": "active", "retire_on": None,
                                         "replacement": None})
    st["source"] = "catalog"
    if provider == "openai" and api_key:
        live = _openai_shutdown_date(model, api_key)
        if live:
            st.update({"status": "deprecated" if live > datetime.utcnow().strftime("%Y-%m-%d")
                       else "retired", "retire_on": live, "source": "openai:shutdown_date"})
    return st


def deprecation_lookahead(ctx) -> Dict[str, Any]:
    provider = getattr(ctx, "ai_provider", "") or ""
    model = getattr(ctx, "ai_model", "") or ""
    st = lifecycle_for(provider, model, getattr(ctx, "ai_api_key", "") or "")
    status, retire_on, repl = st["status"], st.get("retire_on"), st.get("replacement")
    ok, cls, detail = True, None, f"{status}"
    today = datetime.utcnow().date()
    if status == "retired":
        ok, cls = False, DEPRECATED_MODEL
        detail = f"{model} is RETIRED (since {retire_on}); replacement: {repl or 'see catalog'}"
    elif status == "deprecated":
        days = (datetime.fromisoformat(retire_on).date() - today).days if retire_on else None
        if days is None or days <= max(LOOKAHEAD_DAYS):
            ok, cls = False, DEPRECATED_MODEL
            when = f"in {days} days ({retire_on})" if days is not None else "date not announced"
            detail = (f"{model} is DEPRECATED — retires {when}; replacement: "
                      f"{repl or 'see catalog'}; source {st['source']}")
    return _record_transition(getattr(ctx, "profile_id", 0), "deprecation", provider, model,
                              ok, cls, detail, user_id=getattr(ctx, "user_id", 1),
                              profile_name=getattr(ctx, "display_name", "") or "")


# ---------------------------------------------------------------------
# 3. Decision-flow watchdog
# ---------------------------------------------------------------------

def _market_minutes_since(ts_iso: str, now: Optional[datetime] = None) -> float:
    """Regular-session minutes between ts and now (ET, Mon–Fri 09:30–16:00;
    holidays counted as open — conservative: a holiday can only delay
    the alert by one day)."""
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    try:
        t = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    except ValueError:
        return float("inf")
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    t = t.astimezone(et)
    now = (now or datetime.now(timezone.utc)).astimezone(et)
    minutes = 0.0
    cur = t
    while cur < now:
        day_open = cur.replace(hour=9, minute=30, second=0, microsecond=0)
        day_close = cur.replace(hour=16, minute=0, second=0, microsecond=0)
        if cur.weekday() < 5:
            start = max(cur, day_open)
            end = min(now, day_close)
            if end > start:
                minutes += (end - start).total_seconds() / 60
        cur = (cur + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return minutes


def last_successful_decision(db_path: str) -> Optional[str]:
    try:
        with closing(sqlite3.connect(db_path)) as c:
            r = c.execute("SELECT MAX(timestamp) FROM ai_cost_ledger WHERE purpose = 'batch_select'").fetchone()
            return r[0] if r and r[0] else None
    except sqlite3.Error as exc:
        logger.debug("last_successful_decision failed: %s", exc)
        return None


def decision_flow_check(ctx, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Alert when the book has had no successful apex call for
    STALL_MARKET_MINUTES of market time. A book that is not scheduled
    to trade (schedule window, kill switch) still produces a ledger row
    per decision cycle, so quiet books are 'ok' only while the market
    is closed."""
    db_path = getattr(ctx, "db_path", None)
    pid = getattr(ctx, "profile_id", 0)
    name = getattr(ctx, "display_name", "") or ""
    provider = getattr(ctx, "ai_provider", "") or ""
    model = getattr(ctx, "ai_model", "") or ""
    last = last_successful_decision(db_path) if db_path else None
    if last is None:
        # A brand-new book has no ledger yet; count from its first row
        # of anything, else from now (never alert on an empty book).
        return _record_transition(pid, "stall", provider, model, True, None,
                                  "no decisions recorded yet", getattr(ctx, "user_id", 1), name)
    mins = _market_minutes_since(last, now)
    if mins > STALL_MARKET_MINUTES:
        return _record_transition(
            pid, "stall", provider, model, False, UNKNOWN,
            f"no successful AI decision for {mins/60:.1f} market hours (last {last[:16]}Z); "
            f"the book is managing exits only", getattr(ctx, "user_id", 1), name)
    return _record_transition(pid, "stall", provider, model, True, None,
                              f"last decision {mins:.0f} market minutes ago",
                              getattr(ctx, "user_id", 1), name)


# ---------------------------------------------------------------------
# 4. Credit runway (fleet)
# ---------------------------------------------------------------------

def fleet_spend_by_provider(db_paths: List[str], days: int = 7) -> Dict[str, float]:
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()
    out: Dict[str, float] = {}
    for p in db_paths:
        try:
            with closing(sqlite3.connect(p)) as c:
                for prov, s in c.execute(
                        "SELECT provider, SUM(estimated_cost_usd) FROM ai_cost_ledger "
                        "WHERE timestamp >= ? GROUP BY provider", (since,)):
                    out[prov] = out.get(prov, 0.0) + float(s or 0)
        except sqlite3.Error:
            continue
    return out


def credit_runway(db_paths: List[str], user_id: int = 1, budget_usd: Optional[float] = None,
                  days: int = 7) -> Dict[str, Any]:
    import config
    budget = float(budget_usd if budget_usd is not None
                   else getattr(config, "FLEET_MONTHLY_AI_BUDGET_USD", 100.0))
    by_prov = fleet_spend_by_provider(db_paths, days)
    total = sum(by_prov.values())
    projected = total / max(days, 1) * 30.0
    share = projected / budget if budget > 0 else 0.0
    detail = (f"trailing {days}d ${total:.2f} → projected ${projected:.2f}/month vs "
              f"${budget:.0f} budget ({share:.0%}); by provider: "
              + ", ".join(f"{k} ${v:.2f}" for k, v in sorted(by_prov.items())))
    ok = share <= RUNWAY_WARN_SHARE
    row = _record_transition(0, "runway", "fleet", "", ok, None if ok else CREDITS, detail,
                             user_id=user_id, profile_name="fleet")
    row.update({"projected_monthly_usd": round(projected, 2), "budget_usd": budget,
                "by_provider": by_prov})
    return row


# ---------------------------------------------------------------------
# Orchestration used by the scheduler
# ---------------------------------------------------------------------

def run_profile_checks(ctx, now: Optional[datetime] = None) -> Dict[str, Dict[str, Any]]:
    """Probe + stall for one profile. Each check is isolated: a crash
    in one is recorded as its own UNKNOWN failure."""
    out = {}
    for kind, fn in (("probe", probe_profile), ("stall", lambda c: decision_flow_check(c, now))):
        try:
            out[kind] = fn(ctx)
        except Exception as exc:
            logger.exception("provider_health %s crashed", kind)
            try:
                out[kind] = _record_transition(getattr(ctx, "profile_id", 0), kind,
                                               getattr(ctx, "ai_provider", "") or "",
                                               getattr(ctx, "ai_model", "") or "",
                                               False, UNKNOWN, f"check crashed: {exc}",
                                               getattr(ctx, "user_id", 1),
                                               getattr(ctx, "display_name", "") or "")
            except Exception:
                pass
    return out


def run_daily_checks(ctxs: List[Any], user_id: int = 1) -> Dict[str, Any]:
    """Deprecation lookahead per profile + fleet credit runway."""
    out: Dict[str, Any] = {"deprecation": {}, "runway": None}
    for ctx in ctxs:
        try:
            out["deprecation"][getattr(ctx, "profile_id", 0)] = deprecation_lookahead(ctx)
        except Exception as exc:
            logger.exception("deprecation lookahead crashed for %s", getattr(ctx, "profile_id", "?"))
    try:
        out["runway"] = credit_runway([c.db_path for c in ctxs if getattr(c, "db_path", None)],
                                      user_id=user_id)
    except Exception:
        logger.exception("credit runway crashed")
    return out
