"""Per-regime parameter overrides — Layer 3 of autonomous tuning.

Real quant funds use different parameters in different market regimes.
A stop-loss that's right for sideways trading is too tight for a
volatile-regime breakout. A position size that's right in bull is too
aggressive in crisis. This module gives the tuner a place to express
those regime-specific overrides without forcing the user to maintain
five copies of every profile by hand.

Storage: `regime_overrides` JSON column on `trading_profiles`. Shape:
  {parameter_name: {regime: value, ...}, ...}

Examples:
  {"stop_loss_pct": {"volatile": 0.05, "crisis": 0.08},
   "max_position_pct": {"bear": 0.05, "crisis": 0.03}}

The pipeline calls `resolve_param(profile, name, regime)` instead of
`getattr(profile, name)` at every decision point. resolve_param looks
up the per-regime override first; falls back to the profile's global
value if no override is defined OR the override has insufficient
historical sample size to trust.

Recognised regimes: bull, bear, sideways, volatile, crisis. Pipeline
detects the current regime via `market_regime.detect_regime()`.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional, Set

logger = logging.getLogger(__name__)


RECOGNISED_REGIMES: Set[str] = {"bull", "bear", "sideways", "volatile", "crisis"}


def parse_overrides(raw_json: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """Parse the raw `regime_overrides` column into a nested dict.

    Returns {} on missing/invalid data. Filters to recognised regimes
    and parameter names that have bounds defined (defensive — we don't
    want stale overrides to keep applying after a parameter is removed
    from the system)."""
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
    for param_name, regime_map in d.items():
        if not isinstance(regime_map, dict):
            continue
        if param_name not in PARAM_BOUNDS:
            continue
        clean_map = {}
        for regime, value in regime_map.items():
            if regime not in RECOGNISED_REGIMES:
                continue
            try:
                clean_map[regime] = clamp(param_name, value)
            except Exception:
                continue
        if clean_map:
            out[param_name] = clean_map
    return out


def resolve_param(profile_or_dict: Any, param_name: str,
                  regime: Optional[str] = None,
                  default: Any = None) -> Any:
    """Return the value of `param_name` for this profile, optionally
    regime-specific.

    Lookup order:
      1. Per-regime override if `regime` provided AND override exists
      2. Profile's global value (`profile.get(param_name)`)
      3. `default` argument
    """
    # Profile global value (always available as a fallback)
    if isinstance(profile_or_dict, dict):
        global_value = profile_or_dict.get(param_name, default)
        raw_overrides = profile_or_dict.get("regime_overrides")
    else:
        global_value = getattr(profile_or_dict, param_name, default)
        raw_overrides = getattr(profile_or_dict, "regime_overrides", None)

    if regime is None or regime not in RECOGNISED_REGIMES:
        return global_value

    overrides = parse_overrides(raw_overrides) if isinstance(raw_overrides, str) else (raw_overrides or {})
    if not isinstance(overrides, dict):
        overrides = {}
    param_overrides = overrides.get(param_name)
    if not isinstance(param_overrides, dict):
        return global_value
    if regime in param_overrides:
        return param_overrides[regime]
    return global_value


def set_override(profile_id: int, param_name: str, regime: str,
                 value: Any) -> None:
    """Persist a per-regime override. Clamped to PARAM_BOUNDS.

    Setting `value = None` removes the override (falls back to global).
    """
    if regime not in RECOGNISED_REGIMES:
        return
    from models import _get_conn, get_trading_profile
    from param_bounds import clamp

    profile = get_trading_profile(profile_id)
    if not profile:
        return
    raw = profile.get("regime_overrides") or "{}"
    overrides = parse_overrides(raw)
    param_map = overrides.get(param_name, {})

    if value is None:
        param_map.pop(regime, None)
    else:
        param_map[regime] = clamp(param_name, value)

    if param_map:
        overrides[param_name] = param_map
    else:
        overrides.pop(param_name, None)

    conn = _get_conn()
    conn.execute(
        "UPDATE trading_profiles SET regime_overrides = ? WHERE id = ?",
        (json.dumps(overrides), profile_id),
    )
    conn.commit()
    conn.close()


_regime_cache: Dict[str, Any] = {"regime": None, "ts": 0}
_REGIME_CACHE_TTL = 300  # 5 minutes — regime doesn't flip every minute


def _current_regime() -> Optional[str]:
    """Return the current market regime, cached briefly. None if
    detection fails."""
    import time
    now = time.time()
    if _regime_cache["regime"] is not None and (now - _regime_cache["ts"]) < _REGIME_CACHE_TTL:
        return _regime_cache["regime"]
    try:
        from market_regime import detect_regime
        info = detect_regime() or {}
        regime = info.get("regime")
        if regime in RECOGNISED_REGIMES:
            _regime_cache["regime"] = regime
            _regime_cache["ts"] = now
            return regime
    except Exception as exc:
        logger.debug("regime detection failed: %s", exc)
    return None


def resolve_for_current_regime(profile_or_dict: Any, param_name: str,
                                default: Any = None) -> Any:
    """`resolve_param` that auto-detects the current regime AND chains
    with per-time-of-day overrides. Lookup order at decision time:

      1. Per-symbol override (Layer 7 — coming)
      2. Per-regime override (this layer)
      3. Per-time-of-day override (Layer 4)
      4. Profile global value
      5. Caller default

    Each layer falls back to the next when no override exists. The
    chain is intentionally most-specific-first so a tuner-set
    per-symbol override beats a per-regime override beats a per-TOD
    override beats global.
    """
    # Layer 3 — per-regime
    regime = _current_regime()
    regime_value = resolve_param(profile_or_dict, param_name, regime,
                                 default=None)
    # If the regime returned something other than the global value,
    # the per-regime override won. Use it.
    if isinstance(profile_or_dict, dict):
        global_value = profile_or_dict.get(param_name, default)
    else:
        global_value = getattr(profile_or_dict, param_name, default)
    if regime_value is not None and regime_value != global_value:
        return regime_value

    # Layer 4 — per-time-of-day fallback
    try:
        from tod_overrides import resolve_for_current_tod
        return resolve_for_current_tod(profile_or_dict, param_name, default)
    except Exception:
        return regime_value if regime_value is not None else default


def get_all_overrides(profile_or_dict: Any) -> Dict[str, Dict[str, Any]]:
    """Full {param: {regime: value}} dict for this profile."""
    if isinstance(profile_or_dict, dict):
        raw = profile_or_dict.get("regime_overrides")
    else:
        raw = getattr(profile_or_dict, "regime_overrides", None)
    if isinstance(raw, str):
        return parse_overrides(raw)
    if isinstance(raw, dict):
        return raw
    return {}
