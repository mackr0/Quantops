"""Create the 13 fresh-start experiment profiles per docs/15 v2.

Idempotent (safe to re-run): if a profile with the same name already
exists for the given user_id, it's UPDATED in place rather than
duplicated. New profiles are created with `alpaca_account_id=NULL`
so the user can wire them to the fresh Alpaca accounts via the
settings UI afterward (no API keys required at script-run time).

Usage:
    # Dry-run — show what would be created/updated
    /opt/quantopsai/venv/bin/python create_experiment_profiles.py

    # Actually create/update
    /opt/quantopsai/venv/bin/python create_experiment_profiles.py --apply

    # Different user
    /opt/quantopsai/venv/bin/python create_experiment_profiles.py \\
        --apply --user-id 2

After running, the operator does the following in the UI:
  1. Create 3 fresh Alpaca paper accounts in the Alpaca dashboard,
     fund them: Acct 1 = $1M, Acct 2 = $1.25M, Acct 3 = $750K.
  2. Add each Alpaca account to QuantOps via /settings → Alpaca Accounts.
  3. On each profile's settings page, set the `alpaca_account_id`
     to one of the 3 accounts per the docs/15 v2 layout.

That's it. Audits will fire warnings until alpaca_account_id is set
on every profile (no broker == no reconciliation), so leaving it
unset is a deliberate WIP signal.
"""
from __future__ import annotations

import argparse
import logging
import sys
from typing import Any, Dict, List

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# THE 13-PROFILE MANIFEST (per docs/15 v2)
# ─────────────────────────────────────────────────────────────────────
#
# Each entry MUST be self-describing. The script never invents a
# default that isn't in this manifest — what you see here is what
# gets written.

PROFILES: List[Dict[str, Any]] = [

    # ── Account 1: Baselines ($1M, 4 × $250K) ─────────────────────────
    {
        "name": "EXP-A1-BuyHoldSPY",
        "market_type": "stocks",  # post-2026-05-20 unified stock universe
        "initial_capital": 250_000.0,
        "strategy_type": "buy_hold",
        # All AI flags moot for buy_hold but set explicitly so the
        # ctx fields populate cleanly.
        "enable_alt_data": 1,
        "enable_meta_model": 1,
        "enable_self_tuning": 1,
        "enable_options": 0,  # buy_hold doesn't trade options
        "enable_short_selling": 0,
        "is_virtual": 1,
        "max_position_pct": 1.0,   # 100% SPY
        "max_total_positions": 1,
    },
    {
        "name": "EXP-A1-RandomA",
        "market_type": "stocks",
        "initial_capital": 250_000.0,
        "strategy_type": "random",
        "enable_alt_data": 1, "enable_meta_model": 1,
        "enable_self_tuning": 1, "enable_options": 0,
        "enable_short_selling": 0,
        "is_virtual": 1,
        "max_position_pct": 0.20,   # equal-weight across 5 picks
        "max_total_positions": 5,
    },
    {
        "name": "EXP-A1-RandomB",
        "market_type": "stocks",
        "initial_capital": 250_000.0,
        "strategy_type": "random",
        "enable_alt_data": 1, "enable_meta_model": 1,
        "enable_self_tuning": 1, "enable_options": 0,
        "enable_short_selling": 0,
        "is_virtual": 1,
        "max_position_pct": 0.20,
        "max_total_positions": 5,
    },
    {
        # THE ANCHOR — every Account 2 ablation compares to this profile.
        "name": "EXP-A1-FullSystemStandard",
        "market_type": "stocks",
        "initial_capital": 250_000.0,
        "strategy_type": "ai",
        "enable_alt_data": 1,
        "enable_meta_model": 1,
        "enable_self_tuning": 1,
        "enable_options": 1,
        "enable_short_selling": 1,
        "is_virtual": 1,
        "max_position_pct": 0.10,
        "max_total_positions": 999,  # effectively uncapped (2026-06-11): the AI
        # decides position count; only BuyHoldSPY (1) and the Randoms (5)
        # keep fixed caps for the life of the experiment.
        "ai_confidence_threshold": 0.60,
    },

    # ── Account 2: Ablations ($1M, 5 × $200K) ────────────────────────
    # Capital reduced from $250K → $200K vs Anchor so the account
    # totals fit Alpaca's $1M paper-account funding cap. Comparison
    # metrics (% return, Sharpe) are capital-invariant for large-caps
    # with percentage-based position sizing, so the 80% capital
    # difference is documented but not a confounder. Only the named
    # flag differs from Anchor on each ablation — that's what makes
    # the delta meaningful.
    {
        "name": "EXP-A2-NoAltData",
        "market_type": "stocks",
        "initial_capital": 200_000.0,
        "strategy_type": "ai",
        "enable_alt_data": 0,   # ← the only knob different from Anchor
        "enable_meta_model": 1,
        "enable_self_tuning": 1,
        "enable_options": 1,
        "enable_short_selling": 1,
        "is_virtual": 1,
        "max_position_pct": 0.10,
        "max_total_positions": 999,  # effectively uncapped (2026-06-11): the AI
        # decides position count; only BuyHoldSPY (1) and the Randoms (5)
        # keep fixed caps for the life of the experiment.
        "ai_confidence_threshold": 0.60,
    },
    {
        "name": "EXP-A2-NoMetaModel",
        "market_type": "stocks",
        "initial_capital": 200_000.0,
        "strategy_type": "ai",
        "enable_alt_data": 1,
        "enable_meta_model": 0,  # ←
        "enable_self_tuning": 1,
        "enable_options": 1,
        "enable_short_selling": 1,
        "is_virtual": 1,
        "max_position_pct": 0.10,
        "max_total_positions": 999,  # effectively uncapped (2026-06-11): the AI
        # decides position count; only BuyHoldSPY (1) and the Randoms (5)
        # keep fixed caps for the life of the experiment.
        "ai_confidence_threshold": 0.60,
    },
    {
        "name": "EXP-A2-NoSelfTuning",
        "market_type": "stocks",
        "initial_capital": 200_000.0,
        "strategy_type": "ai",
        "enable_alt_data": 1,
        "enable_meta_model": 1,
        "enable_self_tuning": 0,  # ←
        "enable_options": 1,
        "enable_short_selling": 1,
        "is_virtual": 1,
        "max_position_pct": 0.10,
        "max_total_positions": 999,  # effectively uncapped (2026-06-11): the AI
        # decides position count; only BuyHoldSPY (1) and the Randoms (5)
        # keep fixed caps for the life of the experiment.
        "ai_confidence_threshold": 0.60,
    },
    {
        "name": "EXP-A2-NoOptions",
        "market_type": "stocks",
        "initial_capital": 200_000.0,
        "strategy_type": "ai",
        "enable_alt_data": 1,
        "enable_meta_model": 1,
        "enable_self_tuning": 1,
        "enable_options": 0,  # ←
        "enable_short_selling": 1,
        "is_virtual": 1,
        "max_position_pct": 0.10,
        "max_total_positions": 999,  # effectively uncapped (2026-06-11): the AI
        # decides position count; only BuyHoldSPY (1) and the Randoms (5)
        # keep fixed caps for the life of the experiment.
        "ai_confidence_threshold": 0.60,
    },
    {
        # COMBINED ablation — tests whether alt-data + meta-model are
        # complementary or redundant.
        "name": "EXP-A2-NoAltData-NoMetaModel",
        "market_type": "stocks",
        "initial_capital": 200_000.0,
        "strategy_type": "ai",
        "enable_alt_data": 0,    # ←
        "enable_meta_model": 0,  # ←
        "enable_self_tuning": 1,
        "enable_options": 1,
        "enable_short_selling": 1,
        "is_virtual": 1,
        "max_position_pct": 0.10,
        "max_total_positions": 999,  # effectively uncapped (2026-06-11): the AI
        # decides position count; only BuyHoldSPY (1) and the Randoms (5)
        # keep fixed caps for the life of the experiment.
        "ai_confidence_threshold": 0.60,
    },

    # ── Account 3: Product candidate + scale ($750K, 4 profiles) ─────
    {
        # THE $25K real-money question.
        # Constrained best-of-all-strategies: small enough to
        # concentrate, but with all signal sources ON.
        "name": "EXP-A3-25K-Candidate",
        "market_type": "stocks",
        "initial_capital": 25_000.0,
        "strategy_type": "ai",
        "enable_alt_data": 1,
        "enable_meta_model": 1,
        "enable_self_tuning": 1,
        "enable_options": 1,           # single-leg only at this size
        "enable_short_selling": 0,     # shorts tie up too much margin
        "is_virtual": 1,
        "max_position_pct": 0.20,      # up to 20% per pick — conviction
        "max_total_positions": 999,  # effectively uncapped (2026-06-11): the AI
        # decides position count; only BuyHoldSPY (1) and the Randoms (5)
        # keep fixed caps for the life of the experiment.
        "ai_confidence_threshold": 0.65,  # slightly higher bar
    },
    {
        # Reproducibility replica — IDENTICAL config, different
        # profile_id so RNG paths diverge. ±5% vs Candidate = signal.
        "name": "EXP-A3-25K-Replica",
        "market_type": "stocks",
        "initial_capital": 25_000.0,
        "strategy_type": "ai",
        "enable_alt_data": 1,
        "enable_meta_model": 1,
        "enable_self_tuning": 1,
        "enable_options": 1,
        "enable_short_selling": 0,
        "is_virtual": 1,
        "max_position_pct": 0.20,
        "max_total_positions": 999,  # effectively uncapped (2026-06-11): the AI
        # decides position count; only BuyHoldSPY (1) and the Randoms (5)
        # keep fixed caps for the life of the experiment.
        "ai_confidence_threshold": 0.65,
    },
    {
        # 10× scaling test — same constraints as Candidate.
        "name": "EXP-A3-250K-ConservativeScale",
        "market_type": "stocks",
        "initial_capital": 250_000.0,
        "strategy_type": "ai",
        "enable_alt_data": 1,
        "enable_meta_model": 1,
        "enable_self_tuning": 1,
        "enable_options": 1,
        "enable_short_selling": 0,
        "is_virtual": 1,
        "max_position_pct": 0.20,
        "max_total_positions": 999,  # effectively uncapped (2026-06-11): the AI
        # decides position count; only BuyHoldSPY (1) and the Randoms (5)
        # keep fixed caps for the life of the experiment.
        "ai_confidence_threshold": 0.65,
    },
    {
        # Aggressive Free — all small-account constraints DROPPED.
        # Upper-bound test: does lifting constraints unlock alpha?
        # Sized to fill Account 3 to the $1M Alpaca cap after the
        # $25K + $25K + $250K from the other three profiles.
        "name": "EXP-A3-700K-AggressiveFree",
        "market_type": "stocks",
        "initial_capital": 700_000.0,
        "strategy_type": "ai",
        "enable_alt_data": 1,
        "enable_meta_model": 1,
        "enable_self_tuning": 1,
        "enable_options": 1,
        "enable_short_selling": 1,     # shorts ON
        "is_virtual": 1,
        "max_position_pct": 0.08,      # smaller per-position to fit 15
        "max_total_positions": 999,  # effectively uncapped (2026-06-11): the AI
        # decides position count; only BuyHoldSPY (1) and the Randoms (5)
        # keep fixed caps for the life of the experiment.
        "ai_confidence_threshold": 0.55,  # lower bar — let it cook
    },
]


# 2026-06-04 — every profile must explicitly carry ai_provider +
# ai_model in source. The schema defaults (anthropic / claude-haiku)
# don't match what the operator actually uses (google / gemini), so
# without this, every fresh-start reset silently reverts AI config and
# 401s on the first cycle (caught on the 2026-06-04 reset, see
# CHANGELOG). Applied via setdefault so per-profile overrides in the
# manifest above still win. Single source of truth — future provider
# switches change ONE line here, not 13.
_AI_PROVIDER_DEFAULTS = {
    "ai_provider": "google",
    # 2026-06-30: upgraded to gemini-3.1-flash-lite (the live cohort's model).
    # Folded in so the rebuild manifest matches reality and the MANIFEST DRIFT
    # check stays clean (step5b also restores the live model, but the manifest
    # is the source of truth for a from-scratch build).
    "ai_model": "gemini-3.1-flash-lite",
}
for _p in PROFILES:
    for _k, _v in _AI_PROVIDER_DEFAULTS.items():
        _p.setdefault(_k, _v)


# Capital integrity check — fails loudly if the manifest doesn't sum
# to the intended $3M, so a typo can't quietly miss target capital.
def _verify_manifest_totals() -> None:
    total = sum(p["initial_capital"] for p in PROFILES)
    expected = 3_000_000.0
    if abs(total - expected) > 0.01:
        raise ValueError(
            f"Manifest totals ${total:,.0f}, expected ${expected:,.0f}. "
            "Edit PROFILES until they sum exactly."
        )
    counts = {}
    for p in PROFILES:
        if p["name"].startswith("EXP-A1"):
            counts["A1"] = counts.get("A1", 0) + 1
        elif p["name"].startswith("EXP-A2"):
            counts["A2"] = counts.get("A2", 0) + 1
        elif p["name"].startswith("EXP-A3"):
            counts["A3"] = counts.get("A3", 0) + 1
    if counts != {"A1": 4, "A2": 5, "A3": 4}:
        raise ValueError(
            f"Profile-name prefixes give counts {counts}; expected "
            "{'A1': 4, 'A2': 5, 'A3': 4} per docs/15 v2."
        )
    log.info("manifest verified: 13 profiles totaling $%s", f"{total:,.0f}")


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
    log.info("EXPERIMENT PROFILE BUILDER (apply=%s, user=%d)",
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
            cap = spec["initial_capital"]
            stype = spec["strategy_type"]
            log.info("  %s %s  $%-10s strategy=%s",
                     marker, spec["name"], f"{int(cap):,}", stype)

    log.info("=" * 70)
    if args.apply:
        log.info("DONE: created=%d  updated=%d",
                 actions["created"], actions["updated"])
        log.info(
            "\nNext steps:\n"
            "  1. Create 3 fresh Alpaca paper accounts in the Alpaca dashboard:\n"
            "       Acct 1 funded $1,000,000\n"
            "       Acct 2 funded $1,000,000\n"
            "       Acct 3 funded $1,000,000\n"
            "       (Alpaca paper accounts cap at $1M each → 3 × $1M = $3M)\n"
            "  2. /settings → Alpaca Accounts → add each one\n"
            "  3. For each EXP-A1-* profile: set alpaca_account_id = Acct 1\n"
            "     For each EXP-A2-* profile: set alpaca_account_id = Acct 2\n"
            "     For each EXP-A3-* profile: set alpaca_account_id = Acct 3\n"
            "  4. Run ./morning_health_check.sh to confirm 13 profiles\n"
            "     discovered + audit_alerts empty\n"
            "  5. Let it run; first measurement window starts day 15"
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
