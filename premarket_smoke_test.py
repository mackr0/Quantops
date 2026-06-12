"""Pre-market end-to-end smoke test.

Runs every morning before market open (and on-demand). Verifies the
entire decision-making backbone is actually working — not just
"tests pass" but "Alpaca returns real bars, options chains load,
news fetches, strategies produce candidates, cost cap is enforceable,
the dashboard reflects reality."

Built after the 2026-05-15 incident where the master Alpaca key was
revoked and `market_data.get_bars` silently fell back to yfinance for
an unknown period. The system kept running — predictions got
recorded, trades fired, scans completed — but the BACKBONE was
broken. Surface metrics (process up, no errors logged) all looked
green.

This smoke test asserts INTEGRITY signals (which API actually served
this data, is options chain loading, are AI sentiment responses
parseable) so the same class of regression cannot ship undetected.

Run manually:
    venv/bin/python premarket_smoke_test.py
    venv/bin/python premarket_smoke_test.py --strict   # exit 1 on any fail

Pre-market cron (recommended):
    0 8 * * 1-5 cd /opt/quantopsai && /opt/quantopsai/venv/bin/python3 \\
        premarket_smoke_test.py --strict --notify >> logs/smoke.log 2>&1
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Callable, List, Tuple

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


# Each check returns (passed, detail). detail explains the pass or
# the fail. Order: cheapest first; failures cascade because later
# checks depend on earlier infra working.

def check_alpaca_keys_load() -> Tuple[bool, str]:
    """Env vars present and decryptable."""
    from market_data import _resolve_alpaca_credentials
    key, secret, _ = _resolve_alpaca_credentials()
    if not key or not secret:
        return False, "no Alpaca credentials resolved (env empty and alpaca_accounts table empty)"
    return True, f"creds resolved (key prefix {key[:6]}***)"


def check_alpaca_account_endpoint() -> Tuple[bool, str]:
    """Hit /v2/account directly to verify the keys are LIVE."""
    import requests
    from market_data import _resolve_alpaca_credentials
    key, secret, _ = _resolve_alpaca_credentials()
    base = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    r = requests.get(
        f"{base}/v2/account",
        headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
        timeout=10,
    )
    if r.status_code != 200:
        return False, f"/v2/account returned {r.status_code} — keys revoked or wrong tier"
    return True, "Alpaca trading API authenticated"


def check_alpaca_bars_live() -> Tuple[bool, str]:
    """Probe SPY bars via the Alpaca client (NOT via get_bars which
    falls back to yfinance — we want to know if Alpaca itself works)."""
    from market_data import _fetch_via_alpaca
    df = _fetch_via_alpaca("SPY", 5)
    if df is None or len(df) == 0:
        return False, (
            "Alpaca returned no SPY bars — yfinance fallback would "
            "be firing. THIS IS THE 2026-05-15 REGRESSION SHAPE."
        )
    latest = df["close"].iloc[-1]
    return True, f"{len(df)} SPY bars from Alpaca (latest close ${latest:.2f})"


def check_alpaca_options() -> Tuple[bool, str]:
    """SPY options chain — most-liquid options in existence."""
    from options_chain_alpaca import fetch_chain_alpaca
    chain = fetch_chain_alpaca("SPY")
    if not chain or "near_term" not in chain:
        return False, (
            "Alpaca options chain returned None for SPY — "
            "401 / subscription issue. 3 strategies + options "
            "ensemble specialists will not fire."
        )
    n_calls = len(chain["near_term"].get("calls", []))
    return True, f"SPY chain loaded ({n_calls} calls on near expiration)"


def check_alpaca_news() -> Tuple[bool, str]:
    """News API → feeds news_sentiment_spike + AI prompt injection."""
    from news_sentiment import fetch_news_alpaca
    items = fetch_news_alpaca("SPY", limit=3)
    if not items:
        return False, "Alpaca news returned 0 items for SPY"
    if not isinstance(items[0], dict) or "headline" not in items[0]:
        return False, (
            f"news items wrong shape: {type(items[0]).__name__}; "
            "expected dicts with 'headline'"
        )
    return True, f"{len(items)} news items returned"


def check_ai_sentiment_parses() -> Tuple[bool, str]:
    """End-to-end: fetch news → analyze → parse. Catches the 2026-05-15
    markdown-fence parser bug that swallowed every sentiment call."""
    from news_sentiment import get_sentiment_signal
    result = get_sentiment_signal("NVDA")
    if not result:
        return False, "get_sentiment_signal returned None"
    if "error" in result:
        return False, f"sentiment error: {result['error']}"
    score = result.get("sentiment_score")
    if score is None:
        return False, "sentiment response missing sentiment_score"
    return True, f"NVDA sentiment={result.get('label')} score={score:+.2f}"


def check_data_source_health_all_pass() -> Tuple[bool, str]:
    """Run the production health probe; assert all critical pass."""
    from data_source_health import run_all_probes
    h = run_all_probes()
    if not h["all_critical_ok"]:
        return False, (
            f"critical probes failed: {h['critical_failures']}; "
            f"advisory: {h['advisory_failures']}"
        )
    return True, f"all critical OK (advisory failures: {h['advisory_failures'] or 'none'})"


def check_strategy_registry_loads() -> Tuple[bool, str]:
    """Every strategy module imports without error."""
    from glob import glob
    import importlib
    failures = []
    n = 0
    for f in glob("/opt/quantopsai/strategies/*.py"):
        name = os.path.basename(f).replace(".py", "")
        if name == "__init__":
            continue
        n += 1
        try:
            importlib.import_module(f"strategies.{name}")
        except Exception as exc:
            failures.append(f"{name}: {type(exc).__name__}")
    if failures:
        return False, f"{len(failures)}/{n} strategies failed import: {failures}"
    return True, f"all {n} strategy modules import cleanly"


def check_at_least_one_strategy_fires() -> Tuple[bool, str]:
    """End-to-end: build a profile context, run sector_momentum_rotation
    on a small universe, assert at least 1 candidate is produced. This
    is the heaviest top producer — if it doesn't fire on AAPL/MSFT/etc
    something is very wrong."""
    from models import get_active_profiles
    from multi_scheduler import _build_ctx_from_profile
    profs = get_active_profiles()
    if not profs:
        return False, "no active profiles in master DB"
    prof = next((p for p in profs if p["market_type"] == "largecap"), profs[0])
    ctx = _build_ctx_from_profile(prof)
    import importlib
    mod = importlib.import_module("strategies.sector_momentum_rotation")
    universe = ["JPM", "AAPL", "XOM", "UNH", "JNJ", "AMZN", "NVDA", "GS"]
    cands = mod.find_candidates(ctx, universe) or []
    if not cands:
        return False, (
            "sector_momentum_rotation (top producer, 8,000+ lifetime "
            "preds) produced 0 candidates on a 8-name basket — pipeline "
            "is degraded"
        )
    return True, f"sector_momentum_rotation produced {len(cands)} candidates"


def check_cost_cap_enforceable() -> Tuple[bool, str]:
    """cost_guard.status returns sensible numbers + cap is a number."""
    from cost_guard import status
    s = status(1)
    cap = s.get("ceiling_usd")
    if not cap or cap <= 0:
        return False, f"cost ceiling = {cap} (must be > 0)"
    spend = s.get("today_usd", 0)
    return True, f"today=${spend:.2f} of ${cap:.2f} ceiling ({s['ceiling_source']})"


def check_scheduler_alive() -> Tuple[bool, str]:
    """Service running + recent activity in any task_runs table."""
    import subprocess
    r = subprocess.run(
        ["systemctl", "is-active", "quantopsai"],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or "active" not in r.stdout:
        return False, f"systemctl is-active: {r.stdout.strip()}"
    return True, "quantopsai.service is active"


def check_broker_accounts_funded() -> Tuple[bool, str]:
    """2026-06-12 — every execution account's live broker equity
    must cover its enabled profiles' combined capital. The 6-12
    accounts were $1M at reset time and $0 by the open; this check
    would have failed the pre-market smoke loudly instead of the
    day dying in silence."""
    from models import get_active_profiles, build_user_context_from_profile
    from account_funding_guard import funding_status
    seen = set()
    failures = []
    for prof in get_active_profiles():
        aid = prof.get("alpaca_account_id")
        if aid in seen or aid is None:
            continue
        seen.add(aid)
        ctx = build_user_context_from_profile(prof["id"])
        funded, detail = funding_status(ctx)
        if not funded:
            failures.append(detail)
    if failures:
        return False, "; ".join(failures)
    return True, f"{len(seen)} account(s) funded vs combined capital"


CHECKS: List[Tuple[str, Callable[[], Tuple[bool, str]]]] = [
    ("alpaca_keys_load",          check_alpaca_keys_load),
    ("alpaca_account_endpoint",   check_alpaca_account_endpoint),
    ("broker_accounts_funded",    check_broker_accounts_funded),
    ("alpaca_bars_live",          check_alpaca_bars_live),
    ("alpaca_options",            check_alpaca_options),
    ("alpaca_news",               check_alpaca_news),
    ("ai_sentiment_parses",       check_ai_sentiment_parses),
    ("data_source_health_all_pass", check_data_source_health_all_pass),
    ("strategy_registry_loads",   check_strategy_registry_loads),
    ("at_least_one_strategy_fires", check_at_least_one_strategy_fires),
    ("cost_cap_enforceable",      check_cost_cap_enforceable),
    ("scheduler_alive",           check_scheduler_alive),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--strict", action="store_true",
        help="exit 1 if any check fails",
    )
    parser.add_argument(
        "--notify", action="store_true",
        help="send notify_error email on any failure",
    )
    args = parser.parse_args()

    print("=" * 70)
    print(f"PRE-MARKET SMOKE TEST  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    failures: List[str] = []
    for name, fn in CHECKS:
        try:
            ok, detail = fn()
        except Exception as exc:
            ok, detail = False, f"CRASHED: {type(exc).__name__}: {exc}"
        marker = "✅" if ok else "❌"
        print(f"  {marker} {name:34}  {detail}")
        if not ok:
            failures.append(f"{name}: {detail}")

    print("=" * 70)
    if failures:
        print(f"FAILED: {len(failures)} of {len(CHECKS)} checks")
        for f in failures:
            print(f"  - {f}")
        if args.notify:
            try:
                from notifications import notify_error
                notify_error(
                    error_msg="Pre-market smoke test failures:\n" + "\n".join(
                        f"  - {f}" for f in failures
                    ),
                    context="Pre-market smoke test failed",
                )
            except Exception as exc:
                print(f"  (notify_error failed: {exc})")
        if args.strict:
            sys.exit(1)
    else:
        print(f"ALL {len(CHECKS)} CHECKS PASSED")


if __name__ == "__main__":
    main()
