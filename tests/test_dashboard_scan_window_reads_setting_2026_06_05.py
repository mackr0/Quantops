"""Dashboard 'Next: Nm' countdown MUST read users.scan_interval_minutes,
not hardcode 15min. The countdown is what the operator uses to verify
the scheduler is honoring their Settings → Scan interval choice; a
hardcoded 900s makes the dashboard lie when the setting is anything
other than 15.

Failure pinned: views.py had `max(0, 900 - elapsed)` in two places
(the dashboard view loop and the /api/scan-status/<pid> endpoint),
so a user with scan_interval_minutes=5 saw "Next: 15m" / "9m" / etc.
even though the scheduler was scanning every 5 minutes.
"""
from __future__ import annotations

import re
from pathlib import Path


VIEWS = (Path(__file__).resolve().parent.parent / "views.py").read_text()


def test_no_hardcoded_900_minus_elapsed_in_views():
    """No `900 - elapsed` pattern anywhere in views.py — that's the
    smoking gun for a hardcoded 15-min scan window."""
    pattern = re.compile(r"900\s*-\s*elapsed")
    matches = pattern.findall(VIEWS)
    assert not matches, (
        "views.py still contains the hardcoded `900 - elapsed` scan "
        "window. Read users.scan_interval_minutes instead (see "
        f"get_scan_interval_minutes in models.py). Matches: {matches}"
    )


def test_dashboard_reads_scan_interval_for_window():
    """The dashboard view loop must derive its window from
    get_scan_interval_minutes, not a literal."""
    # The fix uses _scan_window_sec computed once above the per-
    # profile loop. Pin that it's wired from get_scan_interval_minutes.
    assert "get_scan_interval_minutes" in VIEWS, (
        "views.py must import get_scan_interval_minutes for the "
        "dashboard scan-window calculation"
    )
    assert re.search(
        r"_scan_window_sec\s*=\s*int\(_get_scan_min\(",
        VIEWS,
    ), (
        "views.py dashboard loop must compute _scan_window_sec from "
        "_get_scan_min(...) so the 'Next: Nm' text matches the "
        "operator's Settings choice"
    )


def test_api_scan_status_reads_scan_interval_for_window():
    """The /api/scan-status/<pid> endpoint must compute next_scan_sec
    from the live scan_interval setting, not a literal."""
    # Locate the api_scan_status function body
    m = re.search(
        r"def api_scan_status\(profile_id\):(.*?)\n@",
        VIEWS,
        re.DOTALL,
    )
    body = m.group(1) if m else VIEWS  # fall back to whole file
    assert "get_scan_interval_minutes" in body, (
        "api_scan_status must read get_scan_interval_minutes; without "
        "it the JS-side countdown uses the wrong window"
    )
    assert re.search(
        r"next_scan_sec.*window_sec\s*-\s*elapsed",
        body,
        re.DOTALL,
    ), (
        "api_scan_status must compute next_scan_sec from "
        "(window_sec - elapsed) where window_sec is derived from "
        "the user's scan_interval_minutes setting"
    )
