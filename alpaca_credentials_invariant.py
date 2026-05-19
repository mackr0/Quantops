"""Startup invariant: every enabled trading_profile must have
resolvable Alpaca credentials, and alpaca_accounts must not be empty
when any profile has per-profile keys.

Background (2026-05-19): post-reset, all 13 profiles had per-profile
encrypted keys set but `alpaca_accounts` was empty and no profile had
`alpaca_account_id` linked. Per-profile broker calls still worked
(resolver falls back to per-profile keys when `alpaca_account_id` is
NULL), but the `data_source_health` probes — which read from
`alpaca_accounts` only — failed on every cycle, emailing the
operator. Worse, `market_data._fetch_via_alpaca` silently fell back
to yfinance for every non-profile-context Alpaca data call,
violating the Alpaca-first data-source priority.

The invariant here makes that state impossible to ship: if it
detects the broken configuration, the scheduler refuses to boot.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import List, Tuple


def check_alpaca_credentials(db_path: str = "quantopsai.db",
                              ) -> Tuple[bool, List[str]]:
    """Return (ok, problems).

    ok=False when any of the following is true:
      A. alpaca_accounts is empty AND any trading_profile has a
         non-empty alpaca_api_key_enc (means the operator entered
         per-profile keys but the shared rows / linkage were never
         backfilled — exactly the 2026-05-19 broken state).
      B. Any enabled trading_profile has neither alpaca_account_id
         set nor non-empty per-profile alpaca_api_key_enc (no
         resolvable credentials → trades and probes both fail).

    Both branches print actionable remediation instructions in
    `problems`.
    """
    problems: List[str] = []
    with closing(sqlite3.connect(db_path)) as conn:
        # Tables may not exist on a fresh bootstrap — treat as OK.
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "trading_profiles" not in tables:
            return True, []
        n_accts = 0
        if "alpaca_accounts" in tables:
            n_accts = conn.execute(
                "SELECT COUNT(*) FROM alpaca_accounts "
                "WHERE alpaca_api_key_enc != '' "
                "AND alpaca_api_key_enc IS NOT NULL"
            ).fetchone()[0]

        prof_rows = conn.execute(
            "SELECT id, name, enabled, alpaca_account_id, "
            "alpaca_api_key_enc FROM trading_profiles"
        ).fetchall()

        # Branch A: shared accounts empty, but per-profile keys exist.
        per_profile_keyed = [
            (pid, pname) for pid, pname, _en, _aid, kenc in prof_rows
            if kenc
        ]
        if n_accts == 0 and per_profile_keyed:
            sample = ", ".join(
                f"#{pid} {pname!r}" for pid, pname in per_profile_keyed[:3]
            )
            extra = ("" if len(per_profile_keyed) <= 3
                     else f" (+{len(per_profile_keyed)-3} more)")
            problems.append(
                f"alpaca_accounts is EMPTY but {len(per_profile_keyed)} "
                f"trading_profile(s) have per-profile Alpaca keys: "
                f"{sample}{extra}. The data_source_health probes read "
                "from alpaca_accounts only and will fail every cycle; "
                "non-profile-context Alpaca data calls will silently "
                "fall back to yfinance.\n"
                "Remediation: run "
                "`/opt/quantopsai/venv/bin/python "
                "full_reset_2026_05_18.py --apply` (step 2 + 2b will "
                "backfill alpaca_accounts and link each profile via "
                "alpaca_account_id)."
            )

        # Branch B: enabled profiles with no resolvable credentials.
        unresolvable = []
        for pid, pname, enabled, aid, kenc in prof_rows:
            if not enabled:
                continue
            if aid is None and not kenc:
                unresolvable.append((pid, pname))
        if unresolvable:
            sample = ", ".join(
                f"#{pid} {pname!r}" for pid, pname in unresolvable[:5]
            )
            extra = ("" if len(unresolvable) <= 5
                     else f" (+{len(unresolvable)-5} more)")
            problems.append(
                f"{len(unresolvable)} enabled trading_profile(s) have "
                f"no resolvable Alpaca credentials (alpaca_account_id "
                f"is NULL and per-profile alpaca_api_key_enc is empty)"
                f": {sample}{extra}.\n"
                "Remediation: either set alpaca_account_id to an "
                "existing alpaca_accounts.id via the Settings UI, or "
                "enter per-profile keys on the profile edit page."
            )

    return (not problems), problems
