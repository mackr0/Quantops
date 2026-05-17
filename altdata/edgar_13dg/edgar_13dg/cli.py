"""Thin CLI shim — real implementation in repo-root sec_13dg_activist.py."""
from __future__ import annotations

import os
import sys


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] != "daily":
        print("Usage: python -m edgar_13dg.cli daily", file=sys.stderr)
        return 2
    repo_root = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "..", "..",
    ))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    from sec_13dg_activist import scrape_recent_13dg_filings
    summary = scrape_recent_13dg_filings()
    print(
        "edgar_13dg daily: seen={seen} new={new} errors={errors}".format(
            **summary,
        )
    )
    return 0 if summary.get("errors", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
