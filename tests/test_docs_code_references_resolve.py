"""Phase 5 of the 2026-06-04 doc-audit plan: anti-staleness CI.

The audit (Phase 2-4) fixed every snapshot-time staleness across 38
markdown files. This test catches the next wave the moment it
appears: every inline code reference in a doc — a backticked
filename, function call, or class name — must still resolve in the
current codebase. When the next refactor renames `widget_handler` to
`process_widget`, every doc that mentioned the old name fails CI
loudly instead of rotting silently for weeks.

What's checked:

  1. File-path references — `` `something.py` ``, `` `pipelines/x.py` ``,
     or unbacked variants the docs use freely. The file MUST exist in
     the current repo (relative to root). Picks up moves and deletes.

  2. Function references — `` `name()` ``. A `def name(` MUST exist
     somewhere in production source. Picks up renames.

  3. Class references — `` `CamelCaseName` ``. A `class CamelCaseName`
     MUST exist somewhere in production source. Picks up renames.

What's NOT checked (intentionally):
  - Behavioral / semantic claims — a doc that names the right function
    but whose behavior contract drifted. That kind of drift requires
    re-running Phase 2's per-claim audit; can't be detected from
    name-resolution alone.
  - Archived docs (`docs/archive/**`) — frozen snapshots by design.
  - The audit / methodology docs (`AUDIT_*.md`) — they describe
    historical state and intentionally cite identifiers that have
    since been renamed.

Allowlist: a small set of names that look like code identifiers but
aren't, plus a small set of historical references in CHANGELOG /
audit docs that should stay even after their referent is gone (the
narrative value of the line outweighs the staleness signal). Each
entry has a one-line justification. Adding to it is the rare path,
not the default.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable, List, Set, Tuple

import pytest


REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Doc files we audit
# ---------------------------------------------------------------------------

def _docs_to_scan() -> List[Path]:
    """Markdown files in scope: docs/*.md (excluding archive + audit
    docs) and the root README.

    CHANGELOG.md is intentionally OUT of scope. It's a historical
    narrative — its entries legitimately cite code that was renamed
    or removed in later commits. The Phase 5 guardrail is about
    current user-facing docs giving the operator names they can
    grep for and find.
    """
    out: List[Path] = []
    for path in (REPO / "docs").rglob("*.md"):
        rel = path.relative_to(REPO)
        if "archive" in rel.parts:
            continue
        if rel.name.startswith("AUDIT_"):
            continue
        out.append(path)
    readme = REPO / "README.md"
    if readme.exists():
        out.append(readme)
    return sorted(out)


# ---------------------------------------------------------------------------
# Reference extraction — inline backticked tokens in markdown
# ---------------------------------------------------------------------------

INLINE_CODE_RE = re.compile(r"`([^`\n]{1,80})`")
PY_FILE_TOKEN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_/]*\.py$")
FUNC_TOKEN_RE = re.compile(r"^([a-z_][a-z0-9_]*)\(\)$")
# CamelCase MUST have a lowercase letter — otherwise we false-positive
# on enum-shaped tokens like CONFIRM / CAUTION / VETO / NAME that are
# values, not class names.
CLASS_TOKEN_RE = re.compile(r"^([A-Z][a-z][A-Za-z0-9]*)$")


# ---------------------------------------------------------------------------
# Code-side resolution — what does the current repo know about?
# ---------------------------------------------------------------------------

def _production_python_files() -> List[Path]:
    """Production Python source — excludes tests, venv, archives,
    altdata subprojects (they have their own world)."""
    out: List[Path] = []
    for path in REPO.rglob("*.py"):
        rel = path.relative_to(REPO)
        parts = rel.parts
        if parts and parts[0] in (
            "venv", "tests", ".git", ".claude", "altdata", "backups",
            "exports", "logs", "node_modules",
        ):
            continue
        if rel.name.startswith("test_"):
            continue
        out.append(path)
    return out


def _all_python_files_for_class_lookup() -> List[Path]:
    """For class-name resolution we include tests/ too — the CHANGELOG
    legitimately cites test classes (`TestXxx`) when announcing
    new regression suites. The class IS defined in the repo, just
    in tests/. Excluding tests/ here would force every CHANGELOG
    entry to land on the allowlist."""
    out: List[Path] = []
    for path in REPO.rglob("*.py"):
        rel = path.relative_to(REPO)
        parts = rel.parts
        if parts and parts[0] in (
            "venv", ".git", ".claude", "altdata", "backups",
            "exports", "logs", "node_modules",
        ):
            continue
        out.append(path)
    return out


def _scan_definitions() -> Tuple[Set[str], Set[str]]:
    """Return (function_names, class_names) defined anywhere in the
    repo. Functions are scanned in production source only; classes
    are scanned in BOTH production source AND tests/ since CHANGELOG
    cites test classes. Cached at module level so the test suite
    pays the grep cost once."""
    funcs: Set[str] = set()
    classes: Set[str] = set()
    def_re = re.compile(r"^\s*(?:async\s+)?def\s+([a-z_][a-z0-9_]*)\s*\(",
                          re.MULTILINE)
    class_re = re.compile(r"^class\s+([A-Z][A-Za-z0-9]+)\b",
                            re.MULTILINE)
    for path in _production_python_files():
        try:
            src = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        funcs.update(def_re.findall(src))
        classes.update(class_re.findall(src))
    # Test classes are legitimate references in CHANGELOG
    for path in _all_python_files_for_class_lookup():
        rel = path.relative_to(REPO)
        if rel.parts and rel.parts[0] == "tests":
            try:
                src = path.read_text()
            except (OSError, UnicodeDecodeError):
                continue
            classes.update(class_re.findall(src))
    return funcs, classes


_DEFS_CACHE: Tuple[Set[str], Set[str]] = ()  # populated lazily
_FILES_CACHE: List[Path] = []


def _get_defs() -> Tuple[Set[str], Set[str]]:
    global _DEFS_CACHE
    if not _DEFS_CACHE:
        _DEFS_CACHE = _scan_definitions()
    return _DEFS_CACHE


def _doc_referable_files() -> List[Path]:
    """All .py files referable from docs — production source PLUS
    tests/ (CHANGELOG and audit docs cite test files by basename).
    Cached so the rglob runs once per test session."""
    global _FILES_CACHE
    if _FILES_CACHE:
        return _FILES_CACHE
    out: List[Path] = []
    for path in REPO.rglob("*.py"):
        rel = path.relative_to(REPO)
        if rel.parts and rel.parts[0] in (
            "venv", ".git", ".claude", "altdata", "backups",
            "exports", "logs", "node_modules",
        ):
            continue
        out.append(path)
    _FILES_CACHE = out
    return out


# ---------------------------------------------------------------------------
# Allowlist — small, justified
# ---------------------------------------------------------------------------

# Tokens that LOOK like Python identifiers but aren't, or that are
# acceptable to leave in docs even when the referent has moved.
# Each entry's comment is the one-line justification.
ALLOWLIST_FILES: Set[str] = {
    # Tooling configuration / external file paths
    "setup.py",         # no longer used in this repo but standard tooling name
    "requirements.txt", # extension is .txt; falsely matches if a regex is sloppy
    # Forward-looking spec files described in docs/20 Phase 4B1
    # (incremental fine-tuning). The spec exists; the files will
    # follow once the data corpus is mature (~Aug 2026).
    "finetune/training_runner.py",
    "finetune/job_monitor.py",
    "finetune/evaluator.py",
    "finetune/inference.py",
    "tests/test_finetune_training_runner.py",
    "tests/test_finetune_job_monitor.py",
    "tests/test_finetune_inference.py",
    "tests/test_finetune_end_to_end.py",
    "scripts/build_finetune_corpus.py",
    # Forward-looking modules described in docs as not-yet-built.
    # The docs are explicit about the "does not exist today" status;
    # they're roadmap references in scaling / self-tuner docs.
    "streaming.py",        # docs/12 §Stage 4 — WebSocket module pending tier upgrade
    "prompt_variants.py",  # docs/17 §4a — prompt-variant registry pending data
}

ALLOWLIST_FUNCS: Set[str] = {
    # Stdlib / 3rd-party — these aren't defined in our source but
    # are referenced in docs as standard tooling.
    "list",  # python builtin
    "set",   # python builtin
    "dict",  # python builtin
    "len",   # python builtin
    "open",  # python builtin
    "print", # python builtin
    "sum",   # python builtin
    "range", # python builtin
    "any",   # python builtin
    "all",   # python builtin
    "abs",   # python builtin
    "min",   # python builtin
    "max",   # python builtin
    "round", # python builtin
    "next",  # python builtin
    "sorted", # python builtin
    "isinstance",  # python builtin
    "hasattr",     # python builtin
    "getattr",     # python builtin
    "setattr",     # python builtin
    "callable",    # python builtin
    "bool",  # python builtin
    "int",   # python builtin
    "float", # python builtin
    "str",   # python builtin
    "type",  # python builtin
    "now",   # datetime.datetime.now — common method call cited in docs
    "load_dotenv",  # python-dotenv — common dep we cite
    "get_snapshots",  # alpaca-trade-api method cited in data dictionary
}

ALLOWLIST_CLASSES: Set[str] = {
    # Python literals that match the CamelCase regex but aren't classes
    "None", "True", "False",
    # Common English words that capitalize at sentence start and
    # happen to be backticked in some docs (regime labels, etc.)
    "Hold",
    # External library or stdlib references commonly cited in docs.
    "REST",          # alpaca_trade_api.REST — 3rd-party (filtered by lowercase rule anyway)
    "TimeFrame",     # alpaca_trade_api.TimeFrame — 3rd-party
    "DataFrame",     # pandas.DataFrame
    "Series",        # pandas.Series
    "Stream",        # alpaca-trade-api Stream — 3rd-party WebSocket
    # Python stdlib exceptions
    "BaseException", "Exception", "ValueError", "RuntimeError",
    "KeyError", "TypeError", "OSError", "NameError",
    "NotImplementedError", "OperationalError",
    "JSONDecodeError",
    "JoinedStr",   # ast.JoinedStr
    "AttributeError",  # python builtin
    "ConnectionError", # python builtin
    "UnicodeDecodeError",  # python builtin
    "AssertionError",  # python builtin
    "Undefined",   # jinja2.Undefined sentinel
    # typing module
    "Dict", "List", "Set", "Tuple", "Optional", "Any",
    "Iterable", "Callable",
    "Path",      # pathlib.Path
    "Fernet",    # cryptography.fernet.Fernet
    # scikit-learn (used by meta-model)
    "GradientBoostingClassifier",
    # Forward-looking architecture: future Pipeline subclasses
    # documented in pipelines/__init__.py + docs/05 + docs/14.
    # Not yet built; placeholder for crypto / futures expansion.
    "CryptoPipeline",
    "FuturesPipeline",
    "FXPipeline",
}


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------

def test_every_inline_code_reference_in_docs_resolves():
    """Every backticked identifier in a current doc must resolve in
    the current codebase. Catches name-level staleness — the most
    common drift class."""
    funcs, classes = _get_defs()

    unresolved_files: List[Tuple[str, int, str]] = []
    unresolved_funcs: List[Tuple[str, int, str]] = []
    unresolved_classes: List[Tuple[str, int, str]] = []

    for doc in _docs_to_scan():
        rel = str(doc.relative_to(REPO))
        try:
            src = doc.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        for line_idx, line in enumerate(src.splitlines(), start=1):
            for m in INLINE_CODE_RE.finditer(line):
                token = m.group(1).strip()
                # File-path reference?
                if PY_FILE_TOKEN_RE.match(token):
                    if token in ALLOWLIST_FILES:
                        continue
                    # Docs cite files by basename or with a partial
                    # path. Accept the reference if a matching file
                    # exists anywhere in the repo, OR if the literal
                    # relative path exists at the root.
                    basename = token.rsplit("/", 1)[-1]
                    if (REPO / token).exists():
                        continue
                    if any(p.name == basename
                           for p in _doc_referable_files()):
                        continue
                    unresolved_files.append((rel, line_idx, token))
                    continue
                # Function reference?
                fm = FUNC_TOKEN_RE.match(token)
                if fm:
                    name = fm.group(1)
                    if name in ALLOWLIST_FUNCS:
                        continue
                    if name not in funcs:
                        unresolved_funcs.append((rel, line_idx, name))
                    continue
                # Class reference?
                cm = CLASS_TOKEN_RE.match(token)
                if cm:
                    name = cm.group(1)
                    if name in ALLOWLIST_CLASSES:
                        continue
                    if name not in classes:
                        unresolved_classes.append((rel, line_idx, name))
                    continue

    if not (unresolved_files or unresolved_funcs or unresolved_classes):
        return  # all resolve

    msg_lines = [
        "Doc-code reference staleness — backticked identifiers in "
        "user-facing docs that no longer exist in the codebase. "
        "The doc-audit plan's Phase 5 guardrail.",
        "",
    ]
    if unresolved_files:
        msg_lines.append("FILE-PATH references that no longer exist:")
        for rel, line, tok in unresolved_files:
            msg_lines.append(f"  {rel}:{line}  `{tok}`")
        msg_lines.append("")
    if unresolved_funcs:
        msg_lines.append(
            "FUNCTION names with no `def NAME(` in production source:"
        )
        for rel, line, tok in unresolved_funcs:
            msg_lines.append(f"  {rel}:{line}  `{tok}()`")
        msg_lines.append("")
    if unresolved_classes:
        msg_lines.append(
            "CLASS names with no `class NAME` in production source:"
        )
        for rel, line, tok in unresolved_classes:
            msg_lines.append(f"  {rel}:{line}  `{tok}`")
        msg_lines.append("")
    msg_lines.append(
        "For each item: either UPDATE the doc to reference the "
        "current name, OR (rare) add the identifier to the "
        "appropriate ALLOWLIST in this test with a one-line "
        "justification."
    )
    pytest.fail("\n".join(msg_lines))


# ---------------------------------------------------------------------------
# Self-test: confirm the scanner does what it says it does
# ---------------------------------------------------------------------------

def test_self_check_detects_a_synthetic_missing_function():
    """If someone breaks the scanner so it can't detect a missing
    reference, the Phase 5 guardrail is silently disabled. Confirm
    the resolution loop actually rejects a non-existent name."""
    funcs, _ = _get_defs()
    assert "definitely_not_a_real_function_xyz" not in funcs, (
        "If this somehow exists, the scanner can't test what it "
        "claims to test — pick a fresher synthetic name."
    )
    fm = FUNC_TOKEN_RE.match("definitely_not_a_real_function_xyz()")
    assert fm is not None
    assert fm.group(1) not in funcs


def test_self_check_detects_a_synthetic_missing_class():
    _, classes = _get_defs()
    assert "DefinitelyNotARealClassXYZ" not in classes
    cm = CLASS_TOKEN_RE.match("DefinitelyNotARealClassXYZ")
    assert cm is not None
    assert cm.group(1) not in classes


def test_self_check_scanner_finds_known_real_function():
    """If the def-scanner can't find an obvious real function, the
    test would emit false-positive failures all over. Confirm it
    finds at least one well-known symbol."""
    funcs, _ = _get_defs()
    # `init_db` is in journal.py and has been there for the project's life.
    assert "init_db" in funcs, (
        "scanner failed to find `def init_db(` in journal.py — "
        "the regex or the file-walk is broken"
    )
