"""Promote a shadow model to a profile's primary — without a reset.

docs/25 step 5.4 (2026-08-23). Production shape: one primary trades,
challengers shadow the same calls; when a challenger proves itself,
it becomes the primary and the old primary becomes a shadow. The
profile keeps its book, positions, history and tuner state and keeps
trading on the next cycle. What changes hands is the decision-maker —
and because every prediction is attributed to the model that made it
(ai_predictions.ai_model), the track-record block, the meta-model and
the Learning Scoreboard scope to the NEW model from its first cycle;
the old model's record is stated, never blended.

One audited operation; never touches positions or orders.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class PromotionError(ValueError):
    pass


def plan_promotion(profile: Dict[str, Any], provider: str, model: str
                   ) -> Dict[str, Any]:
    """Compute the post-promotion config without writing. Raises
    PromotionError when the target isn't a registered model or is
    already the primary."""
    from ai_providers import PROVIDERS
    if model not in PROVIDERS.get(provider, {}).get("models", {}):
        raise PromotionError(f"{provider}:{model} is not a registered model")
    old_provider = (profile.get("ai_provider") or "").lower()
    old_model = profile.get("ai_model") or ""
    if (old_provider, old_model) == (provider, model):
        raise PromotionError(f"{provider}:{model} is already the primary")
    try:
        shadows = list(json.loads(profile.get("shadow_models") or "[]"))
    except (TypeError, ValueError):
        shadows = []
    new_label = f"{provider}:{model}"
    old_label = f"{old_provider}:{old_model}" if old_model else None
    # The new primary leaves the shadow list (an arm never shadows
    # itself); the old primary joins it so its record keeps accruing.
    shadows = [s for s in shadows if s.split("@", 1)[0] != new_label]
    if old_label and old_label not in shadows:
        shadows.append(old_label)
    try:
        shadow_keys = dict(json.loads(profile.get("shadow_api_keys_enc") or "{}"))
    except (TypeError, ValueError):
        shadow_keys = {}
    primary_key_enc = profile.get("ai_api_key_enc") or ""
    new_key_enc = (primary_key_enc if provider == old_provider
                   else shadow_keys.get(provider, ""))
    if not new_key_enc:
        raise PromotionError(
            f"no key on this profile for provider {provider!r} — add it "
            "to the shadow keys (Settings) before promoting")
    if provider != old_provider and primary_key_enc and old_provider:
        shadow_keys.setdefault(old_provider, primary_key_enc)
    return {
        "ai_provider": provider,
        "ai_model": model,
        "ai_api_key_enc": new_key_enc,
        "shadow_models": json.dumps(shadows),
        "shadow_api_keys_enc": json.dumps(shadow_keys),
        "enable_shadow_eval": 1,
        "_from": old_label,
        "_to": new_label,
    }


def promote(profile_id: int, provider: str, model: str, *,
            reason: str = "", dry_run: bool = False,
            user_id: Optional[int] = None) -> Dict[str, Any]:
    """Make `provider:model` the primary of `profile_id`. Returns the
    applied (or planned) config. Logs an activity entry so the switch
    is visible on the dashboard ticker and auditable."""
    from models import get_user_profiles, update_trading_profile, log_activity
    # Resolve the profile row without assuming which user owns it.
    profile = None
    owner = user_id
    if owner is None:
        from contextlib import closing
        from models import _get_conn
        with closing(_get_conn()) as conn:
            row = conn.execute("SELECT user_id FROM trading_profiles WHERE id=?",
                               (profile_id,)).fetchone()
        owner = row[0] if row else None
    if owner is None:
        raise PromotionError(f"profile {profile_id} not found")
    for p in get_user_profiles(owner):
        if p["id"] == profile_id:
            profile = p
            break
    if profile is None:
        raise PromotionError(f"profile {profile_id} not found for user {owner}")
    planned = plan_promotion(profile, provider, model)
    fields = {k: v for k, v in planned.items() if not k.startswith("_")}
    if dry_run:
        logger.info("promotion DRY-RUN profile %s: %s -> %s", profile_id,
                    planned["_from"], planned["_to"])
        return planned
    update_trading_profile(profile_id, **fields)
    try:
        log_activity(
            profile_id=profile_id, user_id=owner,
            activity_type="model_promoted",
            title=f"Primary model promoted: {planned['_to']}",
            detail=(f"{planned['_from']} -> {planned['_to']}; the previous "
                    f"primary now runs as a shadow. {reason}").strip(),
        )
    except Exception as exc:
        logger.warning("promotion: activity log failed: %s: %s",
                       type(exc).__name__, exc)
    logger.warning("promotion APPLIED profile %s: %s -> %s (%s)", profile_id,
                   planned["_from"], planned["_to"], reason or "no reason given")
    return planned
