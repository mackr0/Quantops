"""Shadow model evaluation — fire candidate models in parallel with the
operational primary call, persist results to ai_shadow_calls, never
affect the operational path.

Wired into `ai_providers.call_ai()` AFTER the primary success path. The
primary return value is unchanged. Shadow calls run on a small daemon
thread pool so a slow shadow model can't backlog the scheduler.

Design contract: any failure in this module (config parse, key decrypt,
provider call, DB write) is swallowed and logged. The operational
pipeline must never see a shadow-eval error.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

import config

logger = logging.getLogger(__name__)


# Bounded pool — daemon threads, won't block process exit. Size 4 is
# enough for 1-3 shadow models per call; bursts queue up but don't
# spawn unbounded threads.
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="shadow-eval")


# ---------------------------------------------------------------------------
# Profile lookup
# ---------------------------------------------------------------------------

def _profile_id_from_db_path(db_path: Optional[str]) -> Optional[int]:
    """Extract the profile_id from a per-profile DB path like
    `.../quantopsai_profile_7.db`. Returns None when no match (CLI
    calls, tests, ad-hoc invocations) — shadow eval simply skips."""
    if not db_path:
        return None
    m = re.search(r"profile_(\d+)\.db$", db_path)
    return int(m.group(1)) if m else None


def _load_shadow_config(profile_id: int) -> Optional[Dict[str, Any]]:
    """Read shadow_eval config for a profile from the user DB. Returns
    None when shadow eval is disabled or the profile can't be found.

    Returned dict shape:
        {
            "models": [{"provider": "google", "model": "gemini-2.0-flash"}, ...],
            "api_keys": {"google": "AIza...", "openai": "sk-..."},
        }
    """
    try:
        from models import get_trading_profile
        from crypto import decrypt
    except ImportError as exc:
        # Module-load failure — shadow eval is entirely dead until the
        # deployment is fixed. ERROR-level because no row will ever be
        # written for this profile until the import is resolved.
        logger.error(
            "shadow eval DISABLED: import failed (models/crypto): %s: %s "
            "— check deployment integrity; every shadow call will be "
            "a no-op until resolved",
            type(exc).__name__, exc,
        )
        return None

    try:
        profile = get_trading_profile(profile_id)
    except (sqlite3.OperationalError, sqlite3.DatabaseError,
            KeyError, ValueError, TypeError, OSError) as exc:
        # Per-call profile lookup failure — shadow eval is silently
        # skipped for this call. WARNING so the failure is visible
        # in journald + the Warnings & Errors page.
        logger.warning(
            "shadow eval: profile lookup failed for %d: %s: %s — "
            "shadow disabled for this call",
            profile_id, type(exc).__name__, exc,
        )
        return None

    if not profile or not profile.get("enable_shadow_eval"):
        return None

    raw_models = profile.get("shadow_models") or "[]"
    raw_keys = profile.get("shadow_api_keys_enc") or "{}"

    try:
        model_list = json.loads(raw_models)
        if not isinstance(model_list, list):
            return None
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        # Stored shadow_models JSON is corrupt — the settings UI saved
        # something it shouldn't have, OR a manual DB edit broke the
        # column. Shadow eval is dead for this profile until fixed.
        logger.warning(
            "shadow eval: shadow_models JSON parse failed for profile %d: "
            "%s: %s — shadow disabled until the settings JSON is repaired",
            profile_id, type(exc).__name__, exc,
        )
        return None

    parsed_models: List[Dict[str, str]] = []
    for entry in model_list:
        if isinstance(entry, str) and ":" in entry:
            provider, _, model = entry.partition(":")
            if provider and model:
                parsed_models.append({"provider": provider, "model": model})

    if not parsed_models:
        return None

    try:
        enc_keys = json.loads(raw_keys)
        if not isinstance(enc_keys, dict):
            enc_keys = {}
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        # Stored encrypted-keys JSON is corrupt; user-supplied keys
        # are unrecoverable until they re-save in settings.
        logger.warning(
            "shadow eval: shadow_api_keys_enc JSON parse failed for "
            "profile %d: %s: %s — user must re-save shadow API keys",
            profile_id, type(exc).__name__, exc,
        )
        enc_keys = {}

    api_keys: Dict[str, str] = {}
    for provider, enc in enc_keys.items():
        if not enc:
            continue
        try:
            api_keys[provider] = decrypt(enc)
        except Exception as exc:
            # Key decrypt failed — encryption key was rotated, or the
            # encrypted blob is corrupted. The shadow call for THIS
            # provider can't run until the user re-saves the key.
            # ERROR because user money is being burned on a misconfig
            # without their knowledge.
            logger.error(
                "shadow eval: key decrypt FAILED for provider=%s "
                "(profile %d): %s: %s — user must re-save this API key "
                "in settings or shadow eval will skip this provider",
                provider, profile_id, type(exc).__name__, exc,
            )

    return {"models": parsed_models, "api_keys": api_keys}


# ---------------------------------------------------------------------------
# Cost cap (per-user, cross-profile, mirrors cost_guard pattern)
# ---------------------------------------------------------------------------

_COST_CAP_LOCK = threading.Lock()


def _shadow_spend_in_db(db_path: str, since_clause: str) -> float:
    """Sum of cost_usd for shadow rows in `db_path` matching the SQL
    `since_clause` (e.g. "timestamp >= '2026-05-16'"). Returns 0.0 on a
    per-DB read failure so one bad DB never blocks the cross-profile
    aggregate — but logs at debug so the failure is discoverable."""
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                f"SELECT COALESCE(SUM(cost_usd), 0) FROM ai_shadow_calls "
                f"WHERE {since_clause}"
            ).fetchone()
            return float(row[0] or 0.0)
        finally:
            conn.close()
    except (sqlite3.OperationalError, sqlite3.DatabaseError,
            ValueError, TypeError, OSError) as exc:
        # Per-DB spend read failed. Cap math silently undercounts —
        # user could blow past their cap because this profile's
        # contribution is missing. WARN so the misconfig surfaces.
        logger.warning(
            "shadow eval: per-DB spend read failed for %s: %s: %s — "
            "cap math will undercount until this DB is readable",
            db_path, type(exc).__name__, exc,
        )
        return 0.0


def shadow_today_spend(user_id: int) -> float:
    """Sum of today's (ET) shadow-eval USD spend across this user's
    profiles. Mirrors cost_guard.today_spend and reuses its
    profile-DB enumeration helper for consistency."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from cost_guard import _user_profile_dbs
    et_today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    total = 0.0
    for db_path in _user_profile_dbs(user_id):
        total += _shadow_spend_in_db(
            db_path, f"timestamp >= '{et_today}'"
        )
    return total


def shadow_trailing_avg(user_id: int, days: int = 7) -> float:
    """Average daily shadow USD spend across this user's profiles
    over the trailing N days. 0 if no history. Mirrors
    cost_guard.trailing_avg_daily_spend."""
    from cost_guard import _user_profile_dbs
    total = 0.0
    for db_path in _user_profile_dbs(user_id):
        total += _shadow_spend_in_db(
            db_path,
            f"timestamp >= datetime('now', '-{int(days)} days')",
        )
    return total / max(days, 1)


def _read_shadow_cap_override(user_id: int) -> Optional[float]:
    """Read the user's `shadow_daily_cost_cap_usd` override. Returns
    None when no override exists; raises only on programming errors —
    expected DB read failures are caught, logged, and treated as "no
    override" so callers fall back to the env-var default."""
    try:
        from models import _get_conn
        from contextlib import closing
        with closing(_get_conn()) as conn:
            row = conn.execute(
                "SELECT shadow_daily_cost_cap_usd FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
    except (sqlite3.OperationalError, sqlite3.DatabaseError,
            KeyError, ValueError, TypeError, OSError) as exc:
        # Cap override read failed — user's setting is invisible to
        # the cap check. Falls back to env-var default, but the user
        # set the override for a reason. WARN so it's findable.
        logger.warning(
            "shadow cap override read failed for user %d: %s: %s — "
            "falling back to env-var default; user override invisible",
            user_id, type(exc).__name__, exc,
        )
        return None
    if not row or row[0] is None:
        return None
    try:
        v = float(row[0])
    except (TypeError, ValueError) as exc:
        # Stored cap value isn't parseable as float — corrupted DB or
        # bad UI write. User-set cap won't be honored.
        logger.warning(
            "shadow cap override unparseable for user %d (value=%r): "
            "%s: %s — user cap not honored; env default used instead",
            user_id, row[0], type(exc).__name__, exc,
        )
        return None
    return v if v > 0 else None


def shadow_daily_cap(user_id: int) -> float:
    """Today's cap for shadow spend. User-set override wins; else
    falls back to the SHADOW_DAILY_COST_CAP_USD env var (default $1/day).
    Mirrors cost_guard.daily_ceiling_usd."""
    override = _read_shadow_cap_override(user_id)
    if override is not None:
        return override
    return float(getattr(config, "SHADOW_DAILY_COST_CAP_USD", 1.0) or 1.0)


def shadow_cap_source(user_id: int) -> str:
    """'user' if the cap is user-set, else 'auto' for the env-var
    default."""
    return "user" if _read_shadow_cap_override(user_id) is not None else "auto"


def shadow_status(user_id: int) -> Dict[str, Any]:
    """Snapshot for the settings page autonomy block. Same shape as
    cost_guard.status so the template can render it the same way."""
    today = shadow_today_spend(user_id)
    cap = shadow_daily_cap(user_id)
    avg = shadow_trailing_avg(user_id)
    return {
        "today_usd": round(today, 4),
        "cap_usd": round(cap, 4),
        "cap_source": shadow_cap_source(user_id),
        "trailing_7d_avg_usd": round(avg, 4),
    }


def _shadow_cap_exceeded(db_path: str, est_cost: float) -> bool:
    """True when running this shadow call would push today's cross-
    profile shadow spend over the user's cap."""
    from cost_guard import user_id_for_db_path
    # user_id_for_db_path already wraps its DB read with specific
    # exception handling and returns None on failure — we don't need
    # to wrap the call ourselves.
    user_id = user_id_for_db_path(db_path)
    if user_id is None:
        # No user attribution available — fall back to the env-var cap
        # against this single profile's spend so the gate still bites.
        cap = float(getattr(config, "SHADOW_DAILY_COST_CAP_USD", 1.0) or 1.0)
        with _COST_CAP_LOCK:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            et_today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
            spent = _shadow_spend_in_db(
                db_path, f"timestamp >= '{et_today}'"
            )
            return (spent + est_cost) > cap

    with _COST_CAP_LOCK:
        cap = shadow_daily_cap(user_id)
        spent = shadow_today_spend(user_id)
        return (spent + est_cost) > cap


# ---------------------------------------------------------------------------
# Agreement scoring
# ---------------------------------------------------------------------------

_SIGNAL_FIELDS = ("signal", "action", "recommendation", "direction")


def _extract_signal(parsed: Any) -> Optional[str]:
    """Pull the primary BUY/SELL/HOLD-style decision out of a parsed
    response. Tolerates the various keys different prompts use. Returns
    an uppercased string or None when nothing recognisable is present.
    """
    if not isinstance(parsed, dict):
        return None
    for key in _SIGNAL_FIELDS:
        v = parsed.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip().upper()
    return None


def _try_parse_json(text: str) -> Optional[Any]:
    """Best-effort JSON parse of a model response. Returns None on
    failure — shadow eval still logs the raw text, just without a
    structured comparison. Logs at debug because malformed responses
    are expected (different models, different obedience to schema)
    and we want them findable without spamming warnings."""
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.debug(
            "shadow eval: response JSON parse failed: %s: %s "
            "(text head: %r)",
            type(exc).__name__, exc, text[:80],
        )
        return None


def _compute_agreement(primary_parsed: Any, shadow_parsed: Any) -> Optional[int]:
    """Return 1 when the shadow model's top-level signal matches the
    primary's, 0 when it differs, None when either side has no
    recognisable signal (so we can't grade)."""
    a = _extract_signal(primary_parsed)
    b = _extract_signal(shadow_parsed)
    if a is None or b is None:
        return None
    return 1 if a == b else 0


# ---------------------------------------------------------------------------
# DB write
# ---------------------------------------------------------------------------

def _write_shadow_row(db_path: str, row: Dict[str, Any]) -> None:
    """Insert one shadow call row. Non-raising."""
    try:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """INSERT INTO ai_shadow_calls
                     (call_id, purpose, provider, model, prompt_hash,
                      prompt_text, raw_response, parsed_signal,
                      latency_ms, input_tokens, output_tokens, cost_usd,
                      error, agreement, primary_provider, primary_model,
                      primary_response, primary_parsed)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row.get("call_id"),
                    row.get("purpose"),
                    row.get("provider"),
                    row.get("model"),
                    row.get("prompt_hash"),
                    row.get("prompt_text"),
                    row.get("raw_response"),
                    row.get("parsed_signal"),
                    row.get("latency_ms"),
                    int(row.get("input_tokens") or 0),
                    int(row.get("output_tokens") or 0),
                    float(row.get("cost_usd") or 0.0),
                    row.get("error"),
                    row.get("agreement"),
                    row.get("primary_provider"),
                    row.get("primary_model"),
                    row.get("primary_response"),
                    row.get("primary_parsed"),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except (sqlite3.OperationalError, sqlite3.DatabaseError,
            sqlite3.IntegrityError, OSError, TypeError, ValueError) as exc:
        # The whole point of shadow eval is to write these rows for
        # later analysis. A failed INSERT is DATA LOSS — the model
        # cost has been spent but the comparison evidence is gone.
        # ERROR so this surfaces immediately, not on the daily email
        # when someone notices empty digest sections.
        logger.error(
            "shadow eval: row INSERT FAILED (data loss) for call_id=%s "
            "provider=%s model=%s: %s: %s — model cost was spent but "
            "the evidence row could not be persisted",
            row.get("call_id"), row.get("provider"), row.get("model"),
            type(exc).__name__, exc,
        )


# ---------------------------------------------------------------------------
# Per-shadow task
# ---------------------------------------------------------------------------

def _run_one_shadow(
    *,
    call_id: str,
    db_path: str,
    purpose: Optional[str],
    prompt: str,
    prompt_hash: str,
    max_tokens: int,
    provider: str,
    model: str,
    api_key: str,
    primary_provider: str,
    primary_model: str,
    primary_response: str,
    primary_parsed: Any,
) -> None:
    """Execute one shadow call and persist its row. Catches every
    exception — never propagates."""
    from ai_pricing import estimate_cost_usd

    started = time.time()
    base_row: Dict[str, Any] = {
        "call_id": call_id,
        "purpose": purpose,
        "provider": provider,
        "model": model,
        "prompt_hash": prompt_hash,
        "prompt_text": prompt,
        "primary_provider": primary_provider,
        "primary_model": primary_model,
        "primary_response": primary_response,
        "primary_parsed": (json.dumps(primary_parsed)
                           if primary_parsed is not None else None),
    }

    # Pre-flight cost cap. Use the same worst-case estimator the
    # operational call uses: input ≈ len(prompt)//3, output = max_tokens.
    est_in = max(1, len(prompt) // 3)
    est_out = max(1, int(max_tokens or 0))
    est_cost = estimate_cost_usd(model, est_in, est_out)
    if _shadow_cap_exceeded(db_path, est_cost):
        base_row["error"] = (
            f"shadow daily cost cap reached (est ${est_cost:.4f})"
        )
        base_row["latency_ms"] = 0
        _write_shadow_row(db_path, base_row)
        return

    try:
        from ai_providers import _call_provider
        response_text, in_tok, out_tok = _call_provider(
            provider, prompt, model, api_key, max_tokens,
        )
    except Exception as exc:
        base_row["error"] = f"{type(exc).__name__}: {exc}"
        base_row["latency_ms"] = int((time.time() - started) * 1000)
        _write_shadow_row(db_path, base_row)
        return

    latency_ms = int((time.time() - started) * 1000)
    cost = estimate_cost_usd(model, in_tok, out_tok)

    # Strip fences and parse. The operational caller's parser sees the
    # stripped form, so we compare the same.
    try:
        from ai_providers import _strip_markdown_fences
        cleaned = _strip_markdown_fences(response_text)
    except (AttributeError, TypeError, ValueError) as exc:
        # The shared strip helper raised on a model response — unusual,
        # since it's plain string manipulation. Falls back to raw text;
        # the parse-JSON step downstream may then fail and produce no
        # signal. WARN so the regression in the strip helper surfaces.
        logger.warning(
            "shadow eval: markdown-fence strip failed (provider=%s model=%s): "
            "%s: %s — falling back to raw response",
            provider, model, type(exc).__name__, exc,
        )
        cleaned = response_text

    shadow_parsed = _try_parse_json(cleaned)
    parsed_signal = _extract_signal(shadow_parsed)
    agreement = _compute_agreement(primary_parsed, shadow_parsed)

    base_row.update({
        "raw_response": cleaned,
        "parsed_signal": parsed_signal,
        "latency_ms": latency_ms,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "cost_usd": cost,
        "agreement": agreement,
    })
    _write_shadow_row(db_path, base_row)


# ---------------------------------------------------------------------------
# Public entrypoint — called from call_ai() after primary success
# ---------------------------------------------------------------------------

def dispatch_shadow_calls(
    *,
    db_path: Optional[str],
    prompt: str,
    max_tokens: int,
    purpose: Optional[str],
    primary_provider: str,
    primary_model: str,
    primary_response: str,
) -> Optional[str]:
    """Fire shadow model calls for this primary invocation. Returns the
    `call_id` minted for the primary call so the cost ledger row can be
    tagged. Returns None and is a no-op when shadow eval is disabled or
    no profile-id can be extracted from db_path.

    Never raises. The operational path treats a None return as "no
    shadow eval ran" and continues.
    """
    profile_id = _profile_id_from_db_path(db_path)
    if profile_id is None or not db_path:
        return None

    # _load_shadow_config has its own per-step exception handling and
    # returns None on any expected failure path (missing profile, bad
    # JSON, decrypt failure, etc.). A propagating exception here would
    # indicate a real programmer bug — let it crash a shadow call so
    # tests catch it, never the production call_ai path. The caller
    # (call_ai) wraps its dispatch_shadow_calls invocation in its own
    # try/except for that outer guarantee.
    cfg = _load_shadow_config(profile_id)
    if not cfg:
        return None

    call_id = uuid.uuid4().hex
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    primary_parsed = _try_parse_json(primary_response)

    for entry in cfg["models"]:
        provider = entry["provider"]
        model = entry["model"]
        api_key = cfg["api_keys"].get(provider, "")
        if not api_key:
            # No key configured for this provider — record the gap so
            # the daily email can surface "configure your key" hints.
            _write_shadow_row(db_path, {
                "call_id": call_id,
                "purpose": purpose,
                "provider": provider,
                "model": model,
                "prompt_hash": prompt_hash,
                "prompt_text": prompt,
                "primary_provider": primary_provider,
                "primary_model": primary_model,
                "primary_response": primary_response,
                "primary_parsed": (json.dumps(primary_parsed)
                                   if primary_parsed is not None else None),
                "error": "no api key configured for shadow provider",
                "latency_ms": 0,
            })
            continue

        try:
            _POOL.submit(
                _run_one_shadow,
                call_id=call_id,
                db_path=db_path,
                purpose=purpose,
                prompt=prompt,
                prompt_hash=prompt_hash,
                max_tokens=max_tokens,
                provider=provider,
                model=model,
                api_key=api_key,
                primary_provider=primary_provider,
                primary_model=primary_model,
                primary_response=primary_response,
                primary_parsed=primary_parsed,
            )
        except RuntimeError as exc:
            # RuntimeError = ThreadPoolExecutor rejected the submission
            # (pool shut down). Process exit window; nothing to do.
            logger.debug(
                "shadow eval: pool submit rejected for %s/%s: %s: %s",
                provider, model, type(exc).__name__, exc,
            )

    return call_id


# ---------------------------------------------------------------------------
# Read path — used by the daily email
# ---------------------------------------------------------------------------

def fetch_daily_rows(db_path: str, date_str: str) -> List[Dict[str, Any]]:
    """Return all ai_shadow_calls rows for a given ET date. Used by
    notify_shadow_eval_daily to build the summary email."""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
    except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError) as exc:
        # Per-profile daily-email read failed — the user's digest will
        # silently miss this profile's rows. WARN so empty sections
        # in the email have a discoverable cause.
        logger.warning(
            "shadow eval: connect failed for daily rows (%s): %s: %s — "
            "this profile will be missing from the daily digest",
            db_path, type(exc).__name__, exc,
        )
        return []
    try:
        rows = conn.execute(
            "SELECT * FROM ai_shadow_calls WHERE timestamp LIKE ? "
            "ORDER BY timestamp ASC",
            (f"{date_str}%",),
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError as exc:
        # Same as the connect-fail case — silently empty digest section
        # is the symptom; WARN so the cause surfaces.
        logger.warning(
            "shadow eval: daily rows query failed for %s on date %s: "
            "%s: %s — this profile missing from digest",
            db_path, date_str, type(exc).__name__, exc,
        )
        return []
    finally:
        conn.close()


def fetch_recently_resolved_disagreements(
    db_path: str, lookback_days: int = 7,
) -> List[Dict[str, Any]]:
    """Return shadow-eval disagreements from the past `lookback_days`
    whose matching ai_predictions row has since resolved. Used to
    build the "Recently Resolved" section of the daily digest —
    decisions made yesterday or earlier whose outcomes are now known.

    Match logic: for each disagreement row, look up the ai_predictions
    row by (symbol-from-primary-response, timestamp within ±5 minutes)
    where status='resolved' AND actual_return_pct IS NOT NULL.

    Returns enriched dicts with `outcome_return_pct`, `outcome_days`,
    `outcome_status` added.
    """
    try:
        conn = sqlite3.connect(db_path)
    except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError) as exc:
        # Resolved-disagreements section silently missing from email.
        logger.warning(
            "shadow eval: connect failed for recently-resolved (%s): "
            "%s: %s — 'Recently Resolved' section will be empty for "
            "this profile",
            db_path, type(exc).__name__, exc,
        )
        return []
    try:
        conn.row_factory = sqlite3.Row
        try:
            shadow_rows = conn.execute(
                "SELECT * FROM ai_shadow_calls "
                "WHERE agreement = 0 "
                "AND timestamp >= datetime('now', '-' || ? || ' days') "
                "ORDER BY timestamp DESC",
                (int(lookback_days),),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            # Disagreement query failed (schema drift or DB lock) —
            # 'Recently Resolved' section silently empty.
            logger.warning(
                "shadow eval: disagreement query failed for %s: %s: %s "
                "— 'Recently Resolved' section will be empty",
                db_path, type(exc).__name__, exc,
            )
            return []

        enriched: List[Dict[str, Any]] = []
        for r in shadow_rows:
            d = dict(r)
            primary_parsed = _try_parse_json(d.get("primary_response"))
            symbol = None
            if isinstance(primary_parsed, dict):
                symbol = (primary_parsed.get("symbol")
                          or primary_parsed.get("ticker"))
            if not symbol:
                continue

            try:
                pred = conn.execute(
                    "SELECT predicted_signal, actual_outcome, "
                    "       actual_return_pct, days_held, resolved_at, status "
                    "FROM ai_predictions "
                    "WHERE symbol = ? "
                    "AND status = 'resolved' "
                    "AND actual_return_pct IS NOT NULL "
                    "AND ABS(strftime('%s', timestamp) - strftime('%s', ?)) <= 300 "
                    "ORDER BY ABS(strftime('%s', timestamp) - strftime('%s', ?)) "
                    "LIMIT 1",
                    (symbol, d.get("timestamp"), d.get("timestamp")),
                ).fetchone()
            except sqlite3.OperationalError as exc:
                logger.debug(
                    "shadow eval: ai_predictions lookup failed for "
                    "%s on %s: %s: %s",
                    symbol, d.get("timestamp"),
                    type(exc).__name__, exc,
                )
                pred = None

            if not pred:
                continue
            d["outcome_return_pct"] = pred["actual_return_pct"]
            d["outcome_days"] = pred["days_held"]
            d["outcome_status"] = pred["actual_outcome"]
            d["outcome_symbol"] = symbol
            enriched.append(d)

        return enriched
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Verdict scoring — used by the daily email to label which model was
# right when actual outcomes are available.
# ---------------------------------------------------------------------------

# Threshold below which a return is treated as "in the noise" — neither
# side is meaningfully right. Picked at ±1.5% to roughly match typical
# stop-loss / take-profit fractions; below this the verdict is "tie".
_VERDICT_NOISE_PCT = 1.5


def _signal_outcome_quality(signal, return_pct):
    """Did `signal` align with `return_pct`?

    Returns one of:
      'right'   — the call agreed with the actual move
      'wrong'   — the call went the other way
      'neutral' — outcome was within the noise band, no clear judgment
      'unknown' — signal isn't a recognizable directional call
    """
    if return_pct is None:
        return "unknown"
    sig = (signal or "").upper()
    if sig in ("BUY", "STRONG_BUY"):
        if return_pct > _VERDICT_NOISE_PCT:
            return "right"
        if return_pct < -_VERDICT_NOISE_PCT:
            return "wrong"
        return "neutral"
    if sig in ("SELL", "SHORT", "STRONG_SELL"):
        if return_pct < -_VERDICT_NOISE_PCT:
            return "right"
        if return_pct > _VERDICT_NOISE_PCT:
            return "wrong"
        return "neutral"
    if sig == "HOLD":
        if abs(return_pct) < _VERDICT_NOISE_PCT:
            return "right"
        return "wrong"
    return "unknown"


def _decision_value(signal, return_pct):
    """P&L outcome (in %) of taking the action `signal` indicates,
    given the actual return.

    BUY:  position gains `return_pct`.
    SELL/SHORT: position gains `-return_pct` (short profits when price drops).
    HOLD: zero — no position taken, no P&L.

    Returns None for unrecognized signals (so the caller can fall
    back to 'unknown' rather than pretending it's zero)."""
    if return_pct is None:
        return None
    sig = (signal or "").upper()
    if sig in ("BUY", "STRONG_BUY"):
        return float(return_pct)
    if sig in ("SELL", "SHORT", "STRONG_SELL"):
        return -float(return_pct)
    if sig == "HOLD":
        return 0.0
    return None


def verdict_for_disagreement(primary_signal, shadow_signal, return_pct):
    """Plain-English verdict + reason for a disagreement row whose
    outcome is known.

    Compares each side's *decision value* — the P&L of acting on that
    signal given the actual move. The model whose decision value is
    higher by more than the noise band wins.

    Returns a dict:

        {"winner": "primary" | "shadow" | "tie" | "both_wrong" | "unknown",
         "headline": "<one-liner>",
         "reason": "<reasoning sentence>"}

    The headline goes in the email row; the reason explains why.
    """
    p_val = _decision_value(primary_signal, return_pct)
    s_val = _decision_value(shadow_signal, return_pct)

    primary = (primary_signal or "?").upper()
    shadow = (shadow_signal or "?").upper()
    move = (
        f"+{return_pct:.1f}%" if return_pct is not None and return_pct >= 0
        else (f"{return_pct:.1f}%" if return_pct is not None else "?")
    )

    if p_val is None or s_val is None:
        return {
            "winner": "unknown",
            "headline": "Outcome unclear",
            "reason": (
                f"Outcome was {move} but one or both signals don't map "
                f"to a tradable action."
            ),
        }

    diff = p_val - s_val
    primary_phrase = f"taking {primary} would have returned {p_val:+.1f}%"
    shadow_phrase = f"{shadow} would have returned {s_val:+.1f}%"

    # Noise band — disagreements where neither decision moved P&L by
    # more than the threshold are ties.
    if abs(diff) <= _VERDICT_NOISE_PCT:
        # Both wrong (both negative) vs both fine (both positive or zero)
        if p_val < -_VERDICT_NOISE_PCT and s_val < -_VERDICT_NOISE_PCT:
            return {
                "winner": "both_wrong",
                "headline": "Both lost money",
                "reason": (
                    f"Outcome was {move}. {primary_phrase} and "
                    f"{shadow_phrase} — neither saved the operator."
                ),
            }
        return {
            "winner": "tie",
            "headline": "Tie — move was in the noise",
            "reason": (
                f"Outcome was {move}. {primary_phrase}; "
                f"{shadow_phrase}. Difference is inside the "
                f"±{_VERDICT_NOISE_PCT:.1f}% noise band."
            ),
        }

    if diff > 0:
        return {
            "winner": "primary",
            "headline": "Primary was the better call",
            "reason": (
                f"Outcome was {move}. {primary_phrase}; "
                f"{shadow_phrase}. Primary ahead by "
                f"{diff:+.1f} pts."
            ),
        }
    return {
        "winner": "shadow",
        "headline": "Shadow was the better call",
        "reason": (
            f"Outcome was {move}. {shadow_phrase}; "
            f"{primary_phrase}. Shadow ahead by "
            f"{-diff:+.1f} pts."
        ),
    }
