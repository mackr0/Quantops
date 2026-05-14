"""Structural guardrail: every `specialists/*.py` module that
exposes the specialist protocol (NAME / DESCRIPTION / build_prompt /
parse_response) is referenced in `specialists.SPECIALIST_MODULES`
AND is loaded by `discover_specialists()`.

The bug class.
Someone adds `specialists/momentum_breakout.py` exposing the full
specialist protocol but forgets to append the module path to
`SPECIALIST_MODULES`. The specialist is never loaded; the ensemble
runs on N-1 specialists with no log message and no warning. The
consensus shifts (now missing the breakout-specialist's BUY votes),
backtests stop matching production, and operators have no signal
that anything is wrong.

Acceptable patterns:
  1. Module IS in SPECIALIST_MODULES — verified by string match
  2. Module is intentionally not registered (e.g. WIP, deprecated)
     → must start with `_` (so `os.walk` filter skips it) OR be
     listed in `INTENTIONALLY_UNREGISTERED` with a written rationale.
"""
from __future__ import annotations

import importlib
import os
import sys
from typing import List, Set

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPECIALISTS_DIR = os.path.join(REPO_ROOT, "specialists")


# Modules that look like specialists by file convention but are
# intentionally not registered. Each entry needs a written
# rationale; default-deny.
INTENTIONALLY_UNREGISTERED: dict = {
    # Any specialist file starting with `_` is treated as private and
    # excluded by the discovery loop below. Use that convention for
    # WIP/draft specialists rather than this allowlist.
}


def _enumerate_specialist_files() -> List[str]:
    """All `specialists/*.py` files except `__init__.py` and
    underscore-prefixed (`_common.py`, `_draft_*.py`, etc.)."""
    out = []
    for fname in sorted(os.listdir(SPECIALISTS_DIR)):
        if not fname.endswith(".py"):
            continue
        if fname == "__init__.py":
            continue
        if fname.startswith("_"):
            continue
        out.append(fname[:-3])  # drop .py
    return out


def _module_implements_protocol(mod_name: str) -> bool:
    """True iff `specialists.<mod_name>` exposes the protocol —
    NAME (str), DESCRIPTION (str), build_prompt (callable),
    parse_response (callable)."""
    try:
        mod = importlib.import_module(f"specialists.{mod_name}")
    except Exception:
        return False
    name_ok = isinstance(getattr(mod, "NAME", None), str)
    desc_ok = isinstance(getattr(mod, "DESCRIPTION", None), str)
    build_ok = callable(getattr(mod, "build_prompt", None))
    parse_ok = callable(getattr(mod, "parse_response", None))
    return name_ok and desc_ok and build_ok and parse_ok


class TestEverySpecialistIsRegistered:
    """Default-deny: any specialist file that implements the
    protocol must be in SPECIALIST_MODULES (or in
    INTENTIONALLY_UNREGISTERED with rationale).

    Why it's structural.
    The auto-discovery loop in `specialists.discover_specialists`
    walks `SPECIALIST_MODULES` (a hand-curated list), not the
    filesystem. So adding a new file is silent unless someone also
    appends to that list. This test closes that gap by walking the
    filesystem ground-truth and cross-checking."""

    def test_all_protocol_specialists_are_in_registry(self):
        from specialists import SPECIALIST_MODULES
        registered: Set[str] = set()
        for mod_path in SPECIALIST_MODULES:
            # mod_path is "specialists.earnings_analyst" etc
            if not mod_path.startswith("specialists."):
                continue
            registered.add(mod_path[len("specialists."):])

        missing: List[str] = []
        for mod_name in _enumerate_specialist_files():
            if not _module_implements_protocol(mod_name):
                continue
            if mod_name in registered:
                continue
            if mod_name in INTENTIONALLY_UNREGISTERED:
                continue
            missing.append(mod_name)

        if missing:
            details = "\n".join(
                f"  specialists.{m}  — implements protocol but not "
                f"registered" for m in missing
            )
            pytest.fail(
                "Specialist modules implement the protocol "
                "(NAME/DESCRIPTION/build_prompt/parse_response) but "
                "are NOT in SPECIALIST_MODULES, so the ensemble "
                "silently skips them:\n\n" + details + "\n\nFix one of:"
                "\n  1. Append the module path to SPECIALIST_MODULES "
                "in specialists/__init__.py\n"
                "  2. If the module is intentionally not loaded "
                "(WIP/draft), rename it with a `_` prefix\n"
                "  3. Add to INTENTIONALLY_UNREGISTERED with rationale"
            )

    def test_registry_entries_load_successfully(self):
        """All entries in SPECIALIST_MODULES must actually import
        and expose the protocol. Catches typos and broken modules."""
        from specialists import SPECIALIST_MODULES, discover_specialists
        broken: List[str] = []
        for mod_path in SPECIALIST_MODULES:
            try:
                mod = importlib.import_module(mod_path)
            except Exception as exc:
                broken.append(f"{mod_path}: import failed — {exc}")
                continue
            if not callable(getattr(mod, "build_prompt", None)):
                broken.append(f"{mod_path}: missing build_prompt")
            if not callable(getattr(mod, "parse_response", None)):
                broken.append(f"{mod_path}: missing parse_response")
        if broken:
            pytest.fail(
                "Entries in SPECIALIST_MODULES do not import or do "
                "not expose the specialist protocol:\n  "
                + "\n  ".join(broken)
            )
        # Cross-check: discover_specialists() returns one entry per
        # successfully-loaded registered module.
        loaded = discover_specialists()
        loaded_paths = {f"specialists.{m.NAME}" for m in loaded
                        if hasattr(m, "NAME")}
        registered = set(SPECIALIST_MODULES)
        diff = registered - loaded_paths
        # Discovery uses module attribute name, registry uses module
        # path. They must produce identical counts (after normalizing
        # via NAME). If they don't, a registered file's NAME drifted
        # from its filename.
        assert len(loaded) == len(registered), (
            f"discover_specialists() loaded {len(loaded)} but "
            f"SPECIALIST_MODULES has {len(registered)} entries. "
            f"Missing or broken: {diff}"
        )
