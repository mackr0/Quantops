"""2026-08-03 — lint is part of the gate, permanently.

This repo's pre-deploy gate is the pytest suite (there is no CI), so
the only way lint can be guaranteed to run before every deploy is to
make it a test. `ruff check .` under the repo's own ruff.toml (the
committed policy: bug-catching rules only — pyflakes, bugbear, pylint
errors; style-war rules deliberately excluded there) must be clean.

Deliberately NOT wired into sync.sh: deploys stay dumb; quality gates
run before the push, not during the rollout.

If ruff isn't installed in the running environment, this test FAILS
(not skips): a missing linter silently un-enforcing the gate is the
no-broad-except-skip lesson all over again.
"""
from __future__ import annotations

import os
import subprocess
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def test_ruff_clean():
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "."],
        cwd=REPO, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0 and "No module named" in result.stderr:
        raise AssertionError(
            "ruff is not installed in this environment — the lint gate "
            "is silently OFF. Install it: pip install ruff "
            "(it is in requirements.txt)."
        )
    assert result.returncode == 0, (
        "ruff check failed — fix the findings (or, for a genuinely "
        "style-only rule, add it to ruff.toml's documented ignore "
        "list):\n" + result.stdout[-4000:]
    )
