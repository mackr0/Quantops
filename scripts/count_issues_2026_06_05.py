"""One-off: print the deduped /issues breakdown so the operator can
see what's actually firing vs how many raw events the dedup is
collapsing."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__),
)))

from issues_collector import collect_issues

s = collect_issues(since_hours=24)
groups = s["groups"]
total = s["total_events"]
distinct = s["total_groups"]
print(f"Total raw events (last 24h): {total}")
print(f"Distinct groups: {distinct}")
print()
print("Top 25 groups by occurrence:")
for g in sorted(groups, key=lambda x: -x["occurrences"])[:25]:
    msg = g.get("sample_message") or g.get("signature") or "(no message)"
    msg = msg[:180]
    print(f"  x{g['occurrences']:>5}  [{g['level']}]  [{g.get('source','?')}]  {msg}")
