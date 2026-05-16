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
    except Exception as exc:
        logger.debug("shadow eval: import failed: %s", exc)
        return None

    try:
        profile = get_trading_profile(profile_id)
    except Exception as exc:
        logger.debug("shadow eval: profile lookup failed: %s", exc)
        return None

    if not profile or not profile.get("enable_shadow_eval"):
        return None

    raw_models = profile.get("shadow_models") or "[]"
    raw_keys = profile.get("shadow_api_keys_enc") or "{}"

    try:
        model_list = json.loads(raw_models)
        if not isinstance(model_list, list):
            return None
    except Exception:
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
    except Exception:
        enc_keys = {}

    api_keys: Dict[str, str] = {}
    for provider, enc in enc_keys.items():
        if not enc:
            continue
        try:
            api_keys[provider] = decrypt(enc)
        except Exception as exc:
            logger.debug("shadow eval: key decrypt failed for %s: %s",
                         provider, exc)

    return {"models": parsed_models, "api_keys": api_keys}


# ---------------------------------------------------------------------------
# Cost cap (separate from operational cap)
# ---------------------------------------------------------------------------

_COST_CAP_LOCK = threading.Lock()


def _shadow_spend_today(db_path: str) -> float:
    """Sum of estimated_cost_usd for shadow rows logged today (ET).

    Read-only; uses the local profile DB. Returns 0.0 on any error
    (table missing, db locked) so a stale read never blocks shadow
    eval indefinitely.
    """
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        et_today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM ai_shadow_calls "
                "WHERE timestamp >= ?",
                (et_today,),
            ).fetchone()
            return float(row[0] or 0.0)
        finally:
            conn.close()
    except Exception:
        return 0.0


def _shadow_cap_exceeded(db_path: str, est_cost: float) -> bool:
    """True when running this shadow call would push today's shadow
    spend over SHADOW_DAILY_COST_CAP_USD."""
    cap = float(getattr(config, "SHADOW_DAILY_COST_CAP_USD", 1.0) or 1.0)
    with _COST_CAP_LOCK:
        spent = _shadow_spend_today(db_path)
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
    structured comparison."""
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
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
    except Exception as exc:
        logger.debug("shadow eval: row write failed: %s", exc)


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
    except Exception:
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

    try:
        cfg = _load_shadow_config(profile_id)
    except Exception as exc:
        logger.debug("shadow eval: config load failed: %s", exc)
        return None

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
        except Exception as exc:
            logger.debug("shadow eval: submit failed for %s/%s: %s",
                         provider, model, exc)

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
    except Exception:
        return []
    try:
        rows = conn.execute(
            "SELECT * FROM ai_shadow_calls WHERE timestamp LIKE ? "
            "ORDER BY timestamp ASC",
            (f"{date_str}%",),
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
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
    except Exception:
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
        except sqlite3.OperationalError:
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
            except sqlite3.OperationalError:
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
