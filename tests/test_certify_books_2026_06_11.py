"""certify_books.py — the one-command post-reset certification.

Pins that all four checks exist and are wired into main(); the
checks themselves are exercised live (they need broker + prod
DBs). A fresh session runs `venv/bin/python certify_books.py
--since-hours 168` after a reset instead of re-deriving the
2026-06-11 audit by hand.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_all_four_checks_wired():
    src = (REPO / "certify_books.py").read_text()
    for fn in ("check_broker_drift", "check_reconcile",
               "check_decomposition", "check_issues"):
        assert f"def {fn}" in src, f"{fn} missing"
        assert src.count(fn) >= 2, (
            f"{fn} defined but not invoked from main() — a "
            "certification that skips a check certifies nothing."
        )


def test_importable_and_groups_dynamic():
    src = (REPO / "certify_books.py").read_text()
    assert "alpaca_account_id" in src, (
        "Account groups must derive from trading_profiles."
        "alpaca_account_id — hardcoded pid ranges go stale at the "
        "next reset."
    )
    import certify_books  # noqa: F401  (import must not blow up)
