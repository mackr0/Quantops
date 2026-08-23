"""Create the Experiment 2 profiles per docs/25 (Model Selection &
Learning Plan) — twelve arm-profiles: four models × three replicates,
one replicate of every arm on each of the three paper accounts.

Experiment 1's thirteen-profile manifest (docs/15 v2.1: baselines,
ablations, scale arms) lives at git tag `exp1-system-stability-final`;
see docs/26_EXPERIMENTS.md for why it was retired. The Buy-Hold-SPY and
Random baselines are NO LONGER profiles — they are virtual benchmarks
(`virtual_benchmarks.py`, decision D6) created by the reset script.

Design rule (docs/25 step 1): every arm-profile is IDENTICAL except
`ai_provider` / `ai_model` / `shadow_models`. Same capital, same risk
settings, same specialist set, same universe. Anything that differs
between arms other than the model is a confound.

Idempotent (safe to re-run): if a profile with the same name already
exists for the given user_id, it's UPDATED in place rather than
duplicated. New profiles are created with `alpaca_account_id=NULL`;
the reset script links them by the EXP-A{n}- prefix.

Usage:
    /opt/quantopsai/venv/bin/python create_experiment_profiles.py          # dry-run
    /opt/quantopsai/venv/bin/python create_experiment_profiles.py --apply
    /opt/quantopsai/venv/bin/python create_experiment_profiles.py --apply --user-id 2
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any, Dict, List

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# THE ARMS (docs/25 decision D1, 2026-08-23)
# ─────────────────────────────────────────────────────────────────────
#
# Each arm: (name stem, account group, ai_provider, ai_model, shadow list).
# Cross-shadowing is SPECIALIST purposes only (config.SHADOW_PURPOSES,
# decision D5). The Luna arm also shadows gpt-4.1-nano — Experiment 1's
# one real finding — to bridge old evidence to new.

#
# 2026-08-23 (operator): the INCUMBENT must be an arm. gpt-4.1-nano is
# the one model with measured evidence from Experiment 1 (beat the
# primary on specialist verdicts p=0.001; beat haiku head-to-head
# p=0.0005). A new model is only "better" if it beats nano on real
# trades, so nano runs as a full arm; Luna (its successor) runs beside
# it as the hedge against nano's deprecation.
#
# Each arm cross-shadows every OTHER arm on specialist calls.

_ARM_DEFS: List[Dict[str, str]] = [
    {"stem": "NANO", "ai_provider": "openai", "ai_model": "gpt-4.1-nano"},
    {"stem": "LUNA", "ai_provider": "openai", "ai_model": "gpt-5.6-luna"},
    {"stem": "G35LITE", "ai_provider": "google", "ai_model": "gemini-3.5-flash-lite"},
    {"stem": "G37FLASH", "ai_provider": "google", "ai_model": "gemini-3.7-flash"},
]

ARMS: List[Dict[str, Any]] = [
    {
        **arm,
        "shadow_models": [f"{o['ai_provider']}:{o['ai_model']}"
                          for o in _ARM_DEFS if o is not arm],
    }
    for arm in _ARM_DEFS
]

REPLICATES_PER_ARM = 3
ACCOUNT_GROUPS = ["A1", "A2", "A3"]
# Decision D2 (2026-08-23): $250K per replicate, FOUR arms per $1M
# paper account — replicate i of every arm lives on account i. Two
# properties fall out: each account is fully allocated (the cash-
# parity audit requires Σ profile cash == broker cash; a partial
# allocation was flagged as orphan cash every cycle on the first
# reset), and a broker-account event (the 07-07 wipe class) hits every
# arm equally instead of wiping out one arm.
CAPITAL_PER_REPLICATE = 250_000.0

# The settings every replicate shares — the Experiment 1 "FullSystem"
# anchor configuration, unchanged, so Experiment 2's results are
# comparable to the retired anchor.
_COMMON: Dict[str, Any] = {
    "market_type": "stocks",
    "initial_capital": CAPITAL_PER_REPLICATE,
    "strategy_type": "ai",
    "enable_alt_data": 1,
    "enable_meta_model": 1,
    "enable_self_tuning": 1,
    "enable_options": 1,
    "enable_short_selling": 1,
    "is_virtual": 1,
    "max_position_pct": 0.10,
    "max_total_positions": 999,   # the AI decides position count
    # 0-100 integer scale — the SAME scale the AI's confidence and the
    # entry gate use; 45 is the operator-set backstop floor.
    "ai_confidence_threshold": 45,
    "enable_shadow_eval": 1,
}


def _build_profiles() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for arm in ARMS:
        for i in range(1, REPLICATES_PER_ARM + 1):
            group = ACCOUNT_GROUPS[(i - 1) % len(ACCOUNT_GROUPS)]
            spec = dict(_COMMON)
            spec["name"] = f"EXP-{group}-{arm['stem']}-{i}"
            spec["ai_provider"] = arm["ai_provider"]
            spec["ai_model"] = arm["ai_model"]
            spec["shadow_models"] = json.dumps(arm["shadow_models"])
            out.append(spec)
    return out


PROFILES: List[Dict[str, Any]] = _build_profiles()

EXPECTED_TOTAL = CAPITAL_PER_REPLICATE * REPLICATES_PER_ARM * len(ARMS)
EXPECTED_COUNTS = {g: len(ARMS) * (REPLICATES_PER_ARM // len(ACCOUNT_GROUPS))
                   for g in ACCOUNT_GROUPS}


def _verify_manifest_totals() -> None:
    """Fails loudly if the manifest doesn't match the design: nine
    profiles, equal capital, three per account group, every arm
    naming a registered model, every non-model field identical."""
    total = sum(p["initial_capital"] for p in PROFILES)
    if abs(total - EXPECTED_TOTAL) > 0.01:
        raise ValueError(
            f"Manifest totals ${total:,.0f}, expected ${EXPECTED_TOTAL:,.0f}.")
    counts: Dict[str, int] = {}
    for p in PROFILES:
        group = p["name"].split("-")[1]
        counts[group] = counts.get(group, 0) + 1
    if counts != EXPECTED_COUNTS:
        raise ValueError(
            f"Profile-name prefixes give counts {counts}; expected "
            f"{EXPECTED_COUNTS} per docs/25.")
    from ai_providers import PROVIDERS
    for arm in ARMS:
        if arm["ai_model"] not in PROVIDERS[arm["ai_provider"]]["models"]:
            raise ValueError(
                f"arm {arm['stem']}: {arm['ai_provider']}:{arm['ai_model']} "
                "is not in ai_providers.PROVIDERS")
        for entry in arm["shadow_models"]:
            prov, _, mid = entry.partition(":")
            if mid not in PROVIDERS.get(prov, {}).get("models", {}):
                raise ValueError(
                    f"arm {arm['stem']}: shadow {entry} not registered")
    varying = {"name", "ai_provider", "ai_model", "shadow_models"}
    base = {k: v for k, v in PROFILES[0].items() if k not in varying}
    for p in PROFILES[1:]:
        other = {k: v for k, v in p.items() if k not in varying}
        if other != base:
            raise ValueError(
                f"{p['name']} differs from {PROFILES[0]['name']} in a "
                f"non-model field: {set(other.items()) ^ set(base.items())}")
    log.info("manifest verified: %d profiles totaling $%s; arms=%s",
             len(PROFILES), f"{total:,.0f}",
             [f"{a['ai_provider']}:{a['ai_model']}" for a in ARMS])


def _existing_profile_by_name(user_id: int, name: str):
    """Lookup existing profile by (user_id, name). None if missing."""
    from models import get_user_profiles
    for p in get_user_profiles(user_id):
        if p.get("name") == name:
            return p
    return None


def _apply_profile(user_id: int, spec: Dict[str, Any],
                   apply: bool) -> str:
    """Create or update one profile. Returns action label
    ('created' / 'updated' / 'dry-create' / 'dry-update')."""
    from models import create_trading_profile, update_trading_profile

    existing = _existing_profile_by_name(user_id, spec["name"])
    name = spec["name"]
    market_type = spec["market_type"]
    update_fields = {k: v for k, v in spec.items()
                     if k not in ("name", "market_type")}

    if existing:
        if not apply:
            return "dry-update"
        update_trading_profile(existing["id"], **update_fields)
        log.info("  updated pid=%d %s", existing["id"], name)
        return "updated"

    if not apply:
        return "dry-create"
    pid = create_trading_profile(user_id, name, market_type)
    update_trading_profile(pid, **update_fields)
    log.info("  created pid=%d %s", pid, name)
    return "created"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Actually create/update (default: dry-run)")
    ap.add_argument("--user-id", type=int, default=1,
                    help="Which user owns these profiles (default: 1)")
    args = ap.parse_args()

    log.info("=" * 70)
    log.info("EXPERIMENT 2 PROFILE BUILDER (apply=%s, user=%d)",
             args.apply, args.user_id)
    log.info("=" * 70)

    try:
        _verify_manifest_totals()
    except ValueError as exc:
        log.error("Manifest invalid: %s", exc)
        return 2

    actions = {"created": 0, "updated": 0,
               "dry-create": 0, "dry-update": 0}
    for spec in PROFILES:
        action = _apply_profile(args.user_id, spec, args.apply)
        actions[action] = actions.get(action, 0) + 1
        if not args.apply:
            existing = _existing_profile_by_name(args.user_id, spec["name"])
            marker = "[exists]" if existing else "[new]   "
            log.info("  %s %s  $%-10s %s:%s shadows=%s",
                     marker, spec["name"],
                     f"{int(spec['initial_capital']):,}",
                     spec["ai_provider"], spec["ai_model"],
                     spec["shadow_models"])

    log.info("=" * 70)
    if args.apply:
        log.info("DONE: created=%d  updated=%d",
                 actions["created"], actions["updated"])
        log.info(
            "\nNext steps (the reset script does these when run end-to-end):\n"
            "  1. Link EXP-A1-* → account A1, EXP-A2-* → A2, EXP-A3-* → A3\n"
            "  2. Install per-provider AI keys and shadow keys on each profile\n"
            "  3. Create the virtual benchmarks (Buy-Hold-SPY + Random x10)\n"
            "  4. certify_books.py → CERTIFIED CLEAN, then restart services"
        )
    else:
        log.info(
            "DRY-RUN preview: would create=%d  would update=%d",
            actions["dry-create"], actions["dry-update"],
        )
        log.info("Re-run with --apply to execute.")
    log.info("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
