"""Thin CLI shim so altdata/run-altdata-daily.sh picks up edgar_8k.

Real implementation lives at the repo root in `sec_8k_broad.py` (no
need for the multi-file structure edgar_form4 has — the scraper is a
single ~250-line module). The shim exists only to fit the cron's
`python -m <proj>.cli daily` calling convention.
"""
from __future__ import annotations

import sys


def main() -> int:
    """Cron entry point. Supports the same `daily` subcommand the
    other altdata projects expose."""
    if len(sys.argv) < 2 or sys.argv[1] != "daily":
        print("Usage: python -m edgar_8k.cli daily", file=sys.stderr)
        return 2
    # sys.path the repo root so we can import the canonical scraper
    import os
    repo_root = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "..", "..",
    ))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    from sec_8k_broad import scrape_recent_8k_filings
    summary = scrape_recent_8k_filings()
    print(
        "edgar_8k daily: seen={seen} new={new} errors={errors}".format(
            **summary,
        )
    )
    return 0 if summary.get("errors", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
