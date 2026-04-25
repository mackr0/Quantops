"""Per-profile signal weights — Layer 2 of the autonomous tuning system.

Every signal the AI sees has a per-profile weight on a discrete 4-step
ladder. The tuner adjusts weights down when a signal underperforms for
that profile and back up when it recovers. Weights are stored as JSON
in the `signal_weights` column on `trading_profiles`. Missing keys
default to `1.0` (full strength) — so the storage stays sparse and only
deviations from default consume a row.

The four steps are deliberately chosen:
- 1.0 = full strength. Signal is presented to the AI as today.
- 0.7 = "be slightly skeptical of this signal."
- 0.4 = "this signal has been historically weak — discount it."
- 0.0 = omit signal entirely from the prompt.

This is the per-signal analog of how `param_bounds.clamp` constrains
parameter tuning — a small, safe set of allowed values rather than a
continuous slider that could drift to absurd places.

The corresponding prompt-builder integration in `ai_analyst.py` reads
the weight before formatting each signal block. For weight < 1.0 it
includes a "intensity: X" hint so the AI knows to discount the signal
when forming its decision.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# The 4-step weight ladder. Tuner moves one step at a time; the ladder
# is short on purpose so reversal is fast (worst case 9 days from full
# strength to full omission, with 3-day cooldowns between each step).
WEIGHT_LADDER: Tuple[float, ...] = (1.0, 0.7, 0.4, 0.0)


# Canonical list of weightable signals. Each entry is a feature key
# present in `features_json` on `ai_predictions` rows. The tuner uses
# the corresponding `is_active(features_dict)` predicate to decide
# whether a given prediction "had this signal materially present" so it
# can compute a per-signal win rate. Add to this list when introducing
# new alt-data signals.
#
# The shape: (signal_name, display_label, is_active_predicate)
#
# is_active_predicate takes a `features_json`-decoded dict and returns
# True when the signal was meaningfully strong for that prediction.
# These predicates are intentionally conservative: only the upper
# tail counts as "materially present" so the per-signal WR comparison
# is meaningful.

def _truthy(features: Dict[str, Any], key: str) -> bool:
    v = features.get(key)
    if v is None or v == "" or v == 0 or v == "neutral" or v == "flat":
        return False
    return True


def _at_least(features: Dict[str, Any], key: str, threshold: float) -> bool:
    try:
        return float(features.get(key, 0)) >= threshold
    except (TypeError, ValueError):
        return False


WEIGHTABLE_SIGNALS: Tuple[Tuple[str, str, "callable"], ...] = (
    # Alt-data signals
    ("insider_cluster",          "Insider Buying Cluster",
        lambda f: _truthy(f, "insider_cluster")),
    ("insider_direction",        "Insider Net Direction",
        lambda f: f.get("insider_direction") in ("bullish", "bearish")),
    ("short_pct_float",          "Short % of Float (high)",
        lambda f: _at_least(f, "short_pct_float", 15)),
    ("finra_short_vol_ratio",    "FINRA Short Volume Ratio (elevated)",
        lambda f: _at_least(f, "finra_short_vol_ratio", 0.5)),
    ("dark_pool_pct",            "Dark Pool % of Volume (elevated)",
        lambda f: _at_least(f, "dark_pool_pct", 0.5)),
    ("options_signal",           "Options Flow Signal",
        lambda f: _truthy(f, "options_signal")),
    ("put_call_ratio",           "Put/Call Ratio (extreme)",
        lambda f: float(f.get("put_call_ratio", 1.0) or 1.0) >= 1.5
                  or float(f.get("put_call_ratio", 1.0) or 1.0) <= 0.5),
    ("eps_revision_direction",   "Analyst Estimate Revisions",
        lambda f: f.get("eps_revision_direction") in ("up", "down")),
    ("earnings_surprise_streak", "Earnings Surprise Streak",
        lambda f: abs(int(f.get("earnings_surprise_streak", 0) or 0)) >= 2),
    ("congress_direction",       "Congressional Trade Direction",
        lambda f: f.get("congress_direction") in ("bullish", "bearish")),
    ("rel_strength_vs_sector",   "Relative Strength vs Sector",
        lambda f: abs(float(f.get("rel_strength_vs_sector", 0) or 0)) >= 5),
    ("vwap_position",            "VWAP Position (away from VWAP)",
        lambda f: f.get("vwap_position") in ("above", "below")),
    ("political_context",        "Political Context (MAGA Mode)",
        lambda f: _truthy(f, "political_context") or _truthy(f, "maga_mode")),

    # Strategy weights — the modular `strategies/` plugins. Vote keys
    # land in features_json as `vote_<name>`. Listing the headline ones;
    # auto-generated strategies add their own vote_ keys but get their
    # own weight only if explicitly added here. Default behavior for
    # un-listed strategies is full strength (no weighting).
    ("vote_momentum_breakout",   "Strategy: Momentum Breakout",
        lambda f: _truthy(f, "vote_momentum_breakout")),
    ("vote_volume_spike",        "Strategy: Volume Spike",
        lambda f: _truthy(f, "vote_volume_spike")),
    ("vote_mean_reversion",      "Strategy: Mean Reversion",
        lambda f: _truthy(f, "vote_mean_reversion")),
    ("vote_gap_and_go",          "Strategy: Gap & Go",
        lambda f: _truthy(f, "vote_gap_and_go")),
    ("vote_insider_cluster",     "Strategy: Insider Cluster",
        lambda f: _truthy(f, "vote_insider_cluster")),
    ("vote_short_squeeze_setup", "Strategy: Short Squeeze Setup",
        lambda f: _truthy(f, "vote_short_squeeze_setup")),
    ("vote_earnings_drift",      "Strategy: Earnings Drift",
        lambda f: _truthy(f, "vote_earnings_drift")),
    ("vote_news_sentiment_spike", "Strategy: News Sentiment Spike",
        lambda f: _truthy(f, "vote_news_sentiment_spike")),
)


def signal_names() -> Set[str]:
    """Return the canonical set of weightable signal names."""
    return {name for name, _, _ in WEIGHTABLE_SIGNALS}


def display_label(signal_name: str) -> str:
    """Human label for a weightable signal."""
    for name, label, _ in WEIGHTABLE_SIGNALS:
        if name == signal_name:
            return label
    # Fallback for unrecognised names — title-case
    return " ".join(w.capitalize() for w in signal_name.split("_"))


def is_signal_active(signal_name: str, features: Dict[str, Any]) -> bool:
    """Return True if `signal_name` was materially present in this
    prediction's feature set. False for unrecognised signals."""
    for name, _, predicate in WEIGHTABLE_SIGNALS:
        if name == signal_name:
            try:
                return predicate(features)
            except Exception:
                return False
    return False


# ─────────────────────────────────────────────────────────────────────
# Storage helpers (reading/writing the JSON column)
# ─────────────────────────────────────────────────────────────────────

def parse_weights(raw_json: Optional[str]) -> Dict[str, float]:
    """Parse the raw `signal_weights` column value into a dict.
    Returns {} on missing/invalid data — equivalent to all signals
    at default weight 1.0."""
    if not raw_json:
        return {}
    try:
        d = json.loads(raw_json)
        if not isinstance(d, dict):
            return {}
        # Coerce values to floats and clamp to ladder values for safety
        out = {}
        for k, v in d.items():
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            # Snap to nearest ladder value
            snapped = min(WEIGHT_LADDER, key=lambda w: abs(w - fv))
            out[k] = snapped
        return out
    except (ValueError, TypeError):
        return {}


def get_weight(profile_or_dict: Any, signal_name: str) -> float:
    """Return the current weight for `signal_name` on this profile.
    Defaults to 1.0 if the signal isn't in the profile's
    signal_weights dict."""
    if isinstance(profile_or_dict, dict):
        raw = profile_or_dict.get("signal_weights")
    else:
        raw = getattr(profile_or_dict, "signal_weights", None)
    weights = parse_weights(raw) if isinstance(raw, str) else (raw or {})
    if not isinstance(weights, dict):
        weights = parse_weights(weights) if isinstance(weights, str) else {}
    return float(weights.get(signal_name, 1.0))


def set_weight(profile_id: int, signal_name: str, weight: float) -> None:
    """Persist a new weight for one signal on this profile. Snaps
    `weight` to the nearest WEIGHT_LADDER value before writing."""
    snapped = min(WEIGHT_LADDER, key=lambda w: abs(w - float(weight)))
    from models import _get_conn, get_trading_profile
    profile = get_trading_profile(profile_id)
    if not profile:
        return
    raw = profile.get("signal_weights") or "{}"
    weights = parse_weights(raw)
    if snapped == 1.0:
        # Default — strip from storage to keep dict sparse.
        weights.pop(signal_name, None)
    else:
        weights[signal_name] = snapped
    conn = _get_conn()
    conn.execute(
        "UPDATE trading_profiles SET signal_weights = ? WHERE id = ?",
        (json.dumps(weights), profile_id),
    )
    conn.commit()
    conn.close()


def nudge_down(profile_id: int, signal_name: str) -> Optional[float]:
    """Move signal weight one step down the ladder. Returns the new
    weight, or None if already at 0.0."""
    from models import get_trading_profile
    profile = get_trading_profile(profile_id)
    if not profile:
        return None
    current = get_weight(profile, signal_name)
    try:
        idx = WEIGHT_LADDER.index(current)
    except ValueError:
        # Not on the ladder — snap to nearest then nudge.
        idx = WEIGHT_LADDER.index(
            min(WEIGHT_LADDER, key=lambda w: abs(w - current)))
    if idx >= len(WEIGHT_LADDER) - 1:
        return None  # Already at floor
    new_weight = WEIGHT_LADDER[idx + 1]
    set_weight(profile_id, signal_name, new_weight)
    return new_weight


def nudge_up(profile_id: int, signal_name: str) -> Optional[float]:
    """Move signal weight one step up the ladder. Returns the new
    weight, or None if already at 1.0."""
    from models import get_trading_profile
    profile = get_trading_profile(profile_id)
    if not profile:
        return None
    current = get_weight(profile, signal_name)
    try:
        idx = WEIGHT_LADDER.index(current)
    except ValueError:
        idx = WEIGHT_LADDER.index(
            min(WEIGHT_LADDER, key=lambda w: abs(w - current)))
    if idx <= 0:
        return None  # Already at ceiling
    new_weight = WEIGHT_LADDER[idx - 1]
    set_weight(profile_id, signal_name, new_weight)
    return new_weight


def get_all_weights(profile_or_dict: Any) -> Dict[str, float]:
    """Return the full {signal_name: weight} dict for this profile,
    EXCLUDING signals at default 1.0 (which are not stored)."""
    if isinstance(profile_or_dict, dict):
        raw = profile_or_dict.get("signal_weights")
    else:
        raw = getattr(profile_or_dict, "signal_weights", None)
    if isinstance(raw, str):
        return parse_weights(raw)
    if isinstance(raw, dict):
        return {k: float(v) for k, v in raw.items() if float(v) != 1.0}
    return {}


def render_prompt_hint(signal_name: str, weight: float) -> Optional[str]:
    """Return a short hint to inject into the AI prompt when this
    signal is being shown at less-than-full intensity. None when
    weight is 1.0 (no hint needed)."""
    if weight >= 1.0:
        return None
    if weight <= 0.0:
        # Caller should have omitted the signal entirely; return a
        # safety message in case it's still rendering it.
        return f"[{display_label(signal_name)} is currently disabled for this profile]"
    return (
        f"[Note: {display_label(signal_name)} has been historically less "
        f"reliable for this profile (intensity: {weight:.1f}) — discount its "
        f"contribution accordingly.]"
    )
