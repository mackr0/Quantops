"""Per-symbol parameter overrides — Layer 7 of autonomous tuning.

Some symbols behave fundamentally differently from each other. NVDA's
optimal stop-loss is not the same as KO's. A momentum threshold that
works for the average ticker in the universe might be wildly wrong for
TSLA's volatility. This is the most fine-grained tier of the
override stack.

Same architectural pattern as Layer 3 / Layer 4 — JSON column on
`trading_profiles`, parse / resolve / set helpers, falls into the
chain. Difference: the cooldown is 7 days (vs 3 for global / regime /
TOD) because per-symbol samples are smaller and we want to avoid
over-fitting on day-to-day noise.

This is the most-specific tier — when present, it always wins over
regime / TOD / global.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def parse_overrides(raw_json: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """Parse the raw `symbol_overrides` column.

    Returns {} on missing/invalid data. Filters to parameter names with
    bounds defined; clamps values. Symbols are kept as uppercase
    strings (case-normalised).
    """
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
    for param_name, sym_map in d.items():
        if not isinstance(sym_map, dict) or param_name not in PARAM_BOUNDS:
            continue
        clean = {}
        for symbol, value in sym_map.items():
            if not isinstance(symbol, str) or not symbol:
                continue
            try:
                clean[symbol.upper()] = clamp(param_name, value)
            except Exception:
                continue
        if clean:
            out[param_name] = clean
    return out


def resolve_param(profile_or_dict: Any, param_name: str,
                  symbol: Optional[str] = None,
                  default: Any = None) -> Any:
    """Per-symbol override → profile global → caller default."""
    if isinstance(profile_or_dict, dict):
        global_value = profile_or_dict.get(param_name, default)
        raw_overrides = profile_or_dict.get("symbol_overrides")
    else:
        global_value = getattr(profile_or_dict, param_name, default)
        raw_overrides = getattr(profile_or_dict, "symbol_overrides", None)

    if not symbol:
        return global_value

    overrides = parse_overrides(raw_overrides) if isinstance(raw_overrides, str) else (raw_overrides or {})
    if not isinstance(overrides, dict):
        overrides = {}
    param_overrides = overrides.get(param_name)
    if not isinstance(param_overrides, dict):
        return global_value
    return param_overrides.get(symbol.upper(), global_value)


def set_override(profile_id: int, param_name: str, symbol: str,
                  value: Any) -> None:
    """Persist a per-symbol override. Clamped to PARAM_BOUNDS.
    `value=None` removes the override."""
    if not symbol:
        return
    from models import _get_conn, get_trading_profile
    from param_bounds import clamp

    profile = get_trading_profile(profile_id)
    if not profile:
        return
    raw = profile.get("symbol_overrides") or "{}"
    overrides = parse_overrides(raw)
    param_map = overrides.get(param_name, {})
    sym_key = symbol.upper()
    if value is None:
        param_map.pop(sym_key, None)
    else:
        param_map[sym_key] = clamp(param_name, value)
    if param_map:
        overrides[param_name] = param_map
    else:
        overrides.pop(param_name, None)
    conn = _get_conn()
    conn.execute(
        "UPDATE trading_profiles SET symbol_overrides = ? WHERE id = ?",
        (json.dumps(overrides), profile_id),
    )
    conn.commit()
    conn.close()


def get_all_overrides(profile_or_dict: Any) -> Dict[str, Dict[str, Any]]:
    if isinstance(profile_or_dict, dict):
        raw = profile_or_dict.get("symbol_overrides")
    else:
        raw = getattr(profile_or_dict, "symbol_overrides", None)
    if isinstance(raw, str):
        return parse_overrides(raw)
    if isinstance(raw, dict):
        return raw
    return {}
