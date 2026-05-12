"""Guardrail: documented test counts in docs/*.md must not drift
too far from the actual suite size.

History: in May 2026 Mack noticed the QE/RE doc claimed "2,000+
tests, ~180 files" while the actual count was 2,748 tests across
216 files. Stale doc numbers undermine the rest of the doc's
credibility — if THIS number is wrong, what else is wrong?

This guardrail allows mild drift (so we don't force a doc edit on
every test commit) but catches the kind of multi-year drift that
let those numbers go stale. Tolerance is ±10% — generous enough for
~6 months of normal growth between forced updates.

To fix a failure: run `venv/bin/pytest --collect-only -q | tail -1`
to get the current count, then update the documented numbers in
the files listed below.
"""
import os
import re
import subprocess
import sys

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _current_test_count():
    """Run pytest --collect-only to get the canonical count."""
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
    )
    # Last line of stdout is "NNNN tests collected in 0.XXs"
    last = (out.stdout or "").strip().splitlines()[-1] if out.stdout else ""
    m = re.search(r"(\d+)\s+tests?\s+collected", last)
    if not m:
        pytest.skip(
            f"Could not parse pytest --collect-only output: {last!r}"
        )
    return int(m.group(1))


def _current_test_file_count():
    return len([f for f in os.listdir(os.path.join(REPO_ROOT, "tests"))
                if f.endswith(".py") and f != "__init__.py"])


# Files that quote test counts in their human prose. Each entry is a
# regex that captures the quoted number(s). Tolerance lets the docs
# trail by up to 10%.
_DOC_PATTERNS = [
    # 01_EXECUTIVE_SUMMARY.md: "2,748 tests pass"
    ("docs/01_EXECUTIVE_SUMMARY.md",
     re.compile(r"(\d{1,3}(?:,\d{3})*)\s+tests?\s+pass"),
     "test_count"),
    # 04_TECHNICAL_REFERENCE.md: "216 test files" + "2,748 tests"
    ("docs/04_TECHNICAL_REFERENCE.md",
     re.compile(r"(\d+)\s+test files"),
     "file_count"),
    ("docs/04_TECHNICAL_REFERENCE.md",
     re.compile(r"(\d{1,3}(?:,\d{3})*)\s+tests?,\s+zero skipped"),
     "test_count"),
    # 13_QUALITY_RELIABILITY.md: "(216 files, 2,748 tests, zero skipped)"
    ("docs/13_QUALITY_RELIABILITY.md",
     re.compile(r"\((\d+)\s+files,\s+(\d{1,3}(?:,\d{3})*)\s+tests"),
     "both"),
]


def _to_int(s):
    return int(s.replace(",", ""))


class TestDocsTestCountsFresh:
    """Each documented count must be within ±10% of current."""

    def test_documented_counts_within_tolerance(self):
        actual_tests = _current_test_count()
        actual_files = _current_test_file_count()
        tolerance = 0.10
        problems = []

        for relpath, pattern, kind in _DOC_PATTERNS:
            full = os.path.join(REPO_ROOT, relpath)
            if not os.path.exists(full):
                continue
            with open(full) as f:
                text = f.read()
            for match in pattern.finditer(text):
                if kind == "test_count":
                    documented = _to_int(match.group(1))
                    expected = actual_tests
                elif kind == "file_count":
                    documented = _to_int(match.group(1))
                    expected = actual_files
                elif kind == "both":
                    # First group = file count, second = test count
                    file_doc = _to_int(match.group(1))
                    test_doc = _to_int(match.group(2))
                    if abs(file_doc - actual_files) / max(actual_files, 1) > tolerance:
                        problems.append(
                            f"{relpath}: documents {file_doc} test "
                            f"files; actual is {actual_files} "
                            f"(>10% drift)"
                        )
                    if abs(test_doc - actual_tests) / max(actual_tests, 1) > tolerance:
                        problems.append(
                            f"{relpath}: documents {test_doc} tests; "
                            f"actual is {actual_tests} (>10% drift)"
                        )
                    continue

                if abs(documented - expected) / max(expected, 1) > tolerance:
                    label = "test files" if kind == "file_count" else "tests"
                    problems.append(
                        f"{relpath}: documents {documented} {label}; "
                        f"actual is {expected} (>10% drift)"
                    )

        assert not problems, (
            "Documented test counts have drifted >10% from reality. "
            "Update the doc(s) listed below.\n  "
            + "\n  ".join(problems)
        )
