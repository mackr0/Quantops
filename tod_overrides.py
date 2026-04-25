"""Per-time-of-day parameter overrides — Layer 4 of autonomous tuning.

Equity behaviors differ predictably across the trading day:
- Open (09:30–10:30 ET): high vol, gap-dependent, news reaction
- Midday (10:30–14:30 ET): lower vol, mean-reverting, lunch-hour drift
- Close (14:30–16:00 ET): vol picks up, MOC/LOC orders, position-squaring

Same architectural pattern as Layer 3 (per-regime): a JSON column on
`trading_profiles`, a `resolve_param(profile, name, tod)` helper, a
`resolve_for_current_tod()` auto-detect wrapper, and tuner detection
that creates per-bucket overrides when one bucket diverges materially
from the others.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional, Set

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

logger = logging.getLogger(__name__)


# Three intraday buckets in US Eastern Time.
RECOGNISED_TODS: Set[str] = {"open", "midday", "close"}


def _bucket_for_minute(minutes_since_midnight_et: int) -> Optional[str]:
    """Map a minute-of-day value (ET) to a bucket name. None for
    after-hours (the tuner only operates on regular-session activity)."""
    open_start = 9 * 60 + 30   # 09:30
    open_end = 10 * 60 + 30    # 10:30
    midday_end = 14 * 60 + 30  # 14:30
    close_end = 16 * 60        # 16:00
    if open_start <= minutes_since_midnight_et < open_end:
        return "open"
    if open_end <= minutes_since_midnight_et < midday_end:
        return "midday"
    if midday_end <= minutes_since_midnight_et < close_end:
        return "close"
    return None


def parse_overrides(raw_json: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """Parse the raw `tod_overrides` column. Filters to recognised
    buckets and bounded params; clamps values to PARAM_BOUNDS."""
    if not raw_json:
        return {}
    try:
        d = json.loads(raw_json)
    except (ValueError, TypeError):
        return {}
    if not isinstance(d, dict):
        return {}

    from param_bounds import PARAM_BOUNDS, clamp

    out: Dict[str, Dict[str, Any]] = {}
    for param_name, tod_map in d.items():
        if not isinstance(tod_map, dict) or param_name not in PARAM_BOUNDS:
            continue
        clean = {}
        for tod, value in tod_map.items():
            if tod not in RECOGNISED_TODS:
                continue
            try:
                clean[tod] = clamp(param_name, value)
            except Exception:
                continue
        if clean:
            out[param_name] = clean
    return out


def resolve_param(profile_or_dict: Any, param_name: str,
                  tod: Optional[str] = None,
                  default: Any = None) -> Any:
    """Per-TOD override → profile global → caller default."""
    if isinstance(profile_or_dict, dict):
        global_value = profile_or_dict.get(param_name, default)
        raw_overrides = profile_or_dict.get("tod_overrides")
    else:
        global_value = getattr(profile_or_dict, param_name, default)
        raw_overrides = getattr(profile_or_dict, "tod_overrides", None)

    if tod is None or tod not in RECOGNISED_TODS:
        return global_value

    overrides = parse_overrides(raw_overrides) if isinstance(raw_overrides, str) else (raw_overrides or {})
    if not isinstance(overrides, dict):
        overrides = {}
    param_overrides = overrides.get(param_name)
    if not isinstance(param_overrides, dict):
        return global_value
    if tod in param_overrides:
        return param_overrides[tod]
    return global_value


def _current_tod() -> Optional[str]:
    """Return the current intraday bucket based on US Eastern time.
    None outside regular session (or if zoneinfo unavailable)."""
    if ZoneInfo is None:
        return None
    try:
        now_et = datetime.now(ZoneInfo("America/New_York"))
        # Skip weekends
        if now_et.weekday() >= 5:
            return None
        minutes = now_et.hour * 60 + now_et.minute
        return _bucket_for_minute(minutes)
    except Exception as exc:
        logger.debug("TOD detection failed: %s", exc)
        return None


def resolve_for_current_tod(profile_or_dict: Any, param_name: str,
                              default: Any = None) -> Any:
    """`resolve_param` with auto-detected current TOD bucket."""
    tod = _current_tod()
    return resolve_param(profile_or_dict, param_name, tod, default)


def set_override(profile_id: int, param_name: str, tod: str,
                  value: Any) -> None:
    """Persist a per-TOD override. Clamped to PARAM_BOUNDS.
    `value=None` removes the override."""
    if tod not in RECOGNISED_TODS:
        return
    from models import _get_conn, get_trading_profile
    from param_bounds import clamp

    profile = get_trading_profile(profile_id)
    if not profile:
        return
    raw = profile.get("tod_overrides") or "{}"
    overrides = parse_overrides(raw)
    param_map = overrides.get(param_name, {})
    if value is None:
        param_map.pop(tod, None)
    else:
        param_map[tod] = clamp(param_name, value)
    if param_map:
        overrides[param_name] = param_map
    else:
        overrides.pop(param_name, None)
    conn = _get_conn()
    conn.execute(
        "UPDATE trading_profiles SET tod_overrides = ? WHERE id = ?",
        (json.dumps(overrides), profile_id),
    )
    conn.commit()
    conn.close()


def get_all_overrides(profile_or_dict: Any) -> Dict[str, Dict[str, Any]]:
    if isinstance(profile_or_dict, dict):
        raw = profile_or_dict.get("tod_overrides")
    else:
        raw = getattr(profile_or_dict, "tod_overrides", None)
    if isinstance(raw, str):
        return parse_overrides(raw)
    if isinstance(raw, dict):
        return raw
    return {}
