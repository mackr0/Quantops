"""One-shot: force-run the cross-account remediator on a specific
Alpaca account, bypassing the per-process 5-min throttle. Used to
verify the auto-close path works during market hours rather than
waiting for the scheduler's next pass."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__),
)))

from models import get_user_profiles
from virtual_audit import audit_cross_account
from auto_close_broker_orphans import remediate_account_drift

ACCT_ID = int(os.environ.get("ACCT_ID", "14"))

profiles = get_user_profiles(1)
pids = [
    p["id"] for p in profiles
    if p.get("enabled") and p.get("alpaca_account_id") == ACCT_ID
]
print(f"Profiles on account {ACCT_ID}: {pids}")

problems = audit_cross_account(ACCT_ID, pids)
print(f"Drift items detected: {len(problems)}")
for p in problems:
    print(f"  - {p}")

if not problems:
    print("Nothing to remediate.")
    sys.exit(0)

print()
print("Running remediator...")
results = remediate_account_drift(
    alpaca_account_id=ACCT_ID,
    profile_ids=pids,
    problems=problems,
)
print(f"Remediation results: {len(results)} action(s)")
for r in results:
    sym = r["occ_symbol"]
    action = r["action"]
    diff = r["diff_qty"]
    oid = r.get("close_order_id")
    reason = r["reason"]
    print(f"  - {sym}: {action} (diff={diff}, order_id={oid})")
    print(f"      reason: {reason}")
