"""Every displayed UI timestamp must be Eastern-time + labeled (2026-06-26).

All timestamps in the app are stored UTC (SQLite datetime('now'),
datetime.utcnow()/now(timezone.utc).isoformat(), journald). The operator runs
on US/Eastern and needs every *displayed* time in ET with an "ET" label —
trades already do this via the `friendly_time` Jinja filter, but the AI Brain
history ("2026-06-26 17:50:59") and /issues ("2026-06-24T16:20:54") were
rendering raw UTC. This pins the class: a UTC-origin timestamp field must never
be rendered by a raw `[:N]` slice in a template — it must go through
`friendly_time` (full) or `friendly_date` (date-only), which localize to
America/New_York.
"""
from __future__ import annotations

import glob
import os
import re

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))

# Fields that hold a UTC timestamp/datetime (not a market-calendar trading
# date like entry_date/exit_date/r.date, which are already ET bar dates).
_TS_FIELDS = (
    "timestamp", "created_at", "last_update_at", "updated_at", "transitioned_at",
    "detected_at", "snapshot_at", "promoted_at", "shadow_started_at",
    "retired_at", "deprecated_at", "halted_at", "last_seen", "first_seen",
    "set_at", "last_login_at",
)
# `{{ ... <tsfield>[: ... }}` — a raw slice of a timestamp field inside a
# Jinja expression (the exact bug shape: g.last_seen[:19], h.timestamp[:16]).
_RAW_SLICE = re.compile(
    r"\{\{[^}]*\.(?:%s)\s*\[\s*:" % "|".join(_TS_FIELDS)
)


def test_no_template_raw_slices_a_utc_timestamp():
    offenders = []
    for path in sorted(glob.glob(os.path.join(REPO, "templates", "*.html"))):
        text = open(path, encoding="utf-8").read()
        for m in _RAW_SLICE.finditer(text):
            line = text[:m.start()].count("\n") + 1
            offenders.append("%s:%d  %s" % (
                os.path.basename(path), line,
                text[m.start():m.start() + 60].replace("\n", " ")))
    assert not offenders, (
        "UI timestamp(s) rendered as a raw UTC slice instead of |friendly_time "
        "/ |friendly_date (they'll show UTC with no ET label):\n  "
        + "\n  ".join(offenders))


def test_issues_and_brain_use_eastern_filter():
    issues = open(os.path.join(REPO, "templates", "issues.html")).read()
    assert "g.last_seen|friendly_time" in issues
    assert "g.first_seen|friendly_time" in issues
    views = open(os.path.join(REPO, "views.py")).read()
    # The AI Brain history endpoint must ET-localize its timestamp.
    assert 'friendly_time(r["timestamp"])' in views


def test_friendly_time_outputs_eastern_with_label():
    from display_names import friendly_time, friendly_date
    # 17:50:59 UTC == 13:50 EDT on 2026-06-26 (the exact AI Brain example).
    out = friendly_time("2026-06-26T17:50:59")
    assert out.endswith(" ET")
    assert "1:50 PM" in out
    # space-separated DB form (ai_cycles.timestamp) also localizes.
    assert friendly_time("2026-06-24 16:20:54").endswith(" ET")
    # date-only sibling for lifecycle dates.
    assert friendly_date("2026-06-26T03:30:00") == "Jun 26, 2026"
    assert friendly_time(None) == "--"
