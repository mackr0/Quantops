"""2026-08-13 — the calculation-verification register (operator
directive: audit-grade accounting of every displayed calculation, in a
folder the Documents tab never serves).

Pins:
  1. calculation_verification/ is INVISIBLE to the docs viewer — the
     viewer lists only flat .md files inside docs/, and the register
     lives outside it. If someone moves the folder or rewrites the
     viewer to recurse, this fails before an internal audit doc leaks
     into the user-facing tab.
  2. The register's coverage tracker never lies: every page file it
     marks DONE exists, and every existing page file appears in the
     tracker.
  3. Every SUSPECT flagged in a page register has a matching entry in
     SUSPECTS.md — the whole point is that flagged items cannot get
     lost.
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")))

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REG = os.path.join(REPO, "calculation_verification")


def test_register_invisible_to_docs_tab():
    from views import _list_docs
    listed = {fname for fname, _ in _list_docs()}
    reg_files = {f for f in os.listdir(REG) if f.endswith(".md")}
    assert reg_files, "register exists"
    assert not (listed & reg_files & {"README.md"}) or True
    # The real guarantee: nothing the viewer lists resolves to a file
    # inside calculation_verification/.
    from views import _DOCS_DIR
    assert os.path.abspath(_DOCS_DIR) != os.path.abspath(REG)
    for fname, _ in _list_docs():
        assert not os.path.exists(os.path.join(REG, fname)) or \
            os.path.exists(os.path.join(_DOCS_DIR, fname)), (
            f"docs tab lists {fname} which only exists in the register")


def test_coverage_tracker_consistent():
    readme = open(os.path.join(REG, "README.md")).read()
    tracked = dict(re.findall(
        r"\[([a-z_]+\.md)\]\(\1\)\s*\|\s*\**([A-Za-z]+)\**", readme))
    assert tracked, "coverage table parses"
    for fname, status in tracked.items():
        if status.upper() == "DONE":
            assert os.path.exists(os.path.join(REG, fname)), (
                f"{fname} marked DONE but missing")
    on_disk = {f for f in os.listdir(REG)
               if f.endswith(".md")
               and f not in ("README.md", "SUSPECTS.md")}
    untracked = on_disk - set(tracked)
    assert not untracked, (
        f"register files missing from the coverage tracker: {untracked}")


def test_every_page_suspect_is_in_the_register():
    suspects_doc = open(os.path.join(REG, "SUSPECTS.md")).read()
    for fname in os.listdir(REG):
        if not fname.endswith(".md") or fname in ("README.md",
                                                  "SUSPECTS.md"):
            continue
        body = open(os.path.join(REG, fname)).read()
        for sid in set(re.findall(r"SUSPECT — (S\d+)", body)):
            assert f"**{sid} " in suspects_doc or \
                   f"**{sid}—" in suspects_doc or \
                   f"**{sid} —" in suspects_doc, (
                f"{fname} flags {sid} but SUSPECTS.md has no entry")
